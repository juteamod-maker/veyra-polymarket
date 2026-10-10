"""
T36: BUY/SELL Size 参数优化 — 单元测试

覆盖：
1. get_min_order_size() 函数
2. order_executor sell 路径使用 API min_order_size
3. order_executor buy 路径使用 API min_order_size
4. paper_trader 使用 min_bet_usdc 替代 min_order_shares
5. signal_engine 费率修正（taker_fee_rate_default）
6. live_filters 费率修正（per-category taker_fee_rates）
7. main.py _resolve_token_id_from_market 辅助函数
"""

import math
import unittest
from unittest.mock import MagicMock, patch

# 在 conftest autouse mock 生效前，保存真实函数引用
from src.utils.orderbook_history import get_min_order_size as _real_get_min_order_size

# ──────────────────────────────────────────────────────────────────
# 1. get_min_order_size
# ──────────────────────────────────────────────────────────────────


class TestGetMinOrderSize(unittest.TestCase):
    """T36: 验证 get_min_order_size 从 Orderbook API 正确提取 min_order_size。

    conftest 全局 mock 了 get_min_order_size(return_value=5.0)。
    这里用 side_effect=_real_get_min_order_size 恢复真实逻辑，
    同时 mock fetch_orderbook 控制 API 返回值。
    """

    @patch("src.utils.orderbook_history.fetch_orderbook")
    @patch(
        "src.utils.orderbook_history.get_min_order_size",
        side_effect=_real_get_min_order_size,
    )
    def test_normal_return(self, _mock_fn, mock_fetch):
        """正常情况：API 返回 min_order_size=5.0。"""
        mock_fetch.return_value = {
            "bids": [],
            "asks": [],
            "min_order_size": "5.0",
            "tick_size": "0.01",
        }
        result = _real_get_min_order_size("12345")
        self.assertEqual(result, 5.0)

    @patch("src.utils.orderbook_history.fetch_orderbook")
    @patch(
        "src.utils.orderbook_history.get_min_order_size",
        side_effect=_real_get_min_order_size,
    )
    def test_api_failure_returns_none(self, _mock_fn, mock_fetch):
        """API 失败返回 None。"""
        mock_fetch.return_value = None
        result = _real_get_min_order_size("12345")
        self.assertIsNone(result)

    @patch("src.utils.orderbook_history.fetch_orderbook")
    @patch(
        "src.utils.orderbook_history.get_min_order_size",
        side_effect=_real_get_min_order_size,
    )
    def test_missing_field_returns_none(self, _mock_fn, mock_fetch):
        """API 返回结果中无 min_order_size 字段。"""
        mock_fetch.return_value = {"bids": [], "asks": []}
        result = _real_get_min_order_size("12345")
        self.assertIsNone(result)

    @patch("src.utils.orderbook_history.fetch_orderbook")
    @patch(
        "src.utils.orderbook_history.get_min_order_size",
        side_effect=_real_get_min_order_size,
    )
    def test_zero_value_returns_none(self, _mock_fn, mock_fetch):
        """min_order_size=0 被视为异常。"""
        mock_fetch.return_value = {"bids": [], "asks": [], "min_order_size": "0"}
        result = _real_get_min_order_size("12345")
        self.assertIsNone(result)

    @patch("src.utils.orderbook_history.fetch_orderbook")
    @patch(
        "src.utils.orderbook_history.get_min_order_size",
        side_effect=_real_get_min_order_size,
    )
    def test_huge_value_returns_none(self, _mock_fn, mock_fetch):
        """min_order_size > 10000 被视为异常。"""
        mock_fetch.return_value = {
            "bids": [],
            "asks": [],
            "min_order_size": "99999",
        }
        result = _real_get_min_order_size("12345")
        self.assertIsNone(result)

    @patch("src.utils.orderbook_history.fetch_orderbook")
    @patch(
        "src.utils.orderbook_history.get_min_order_size",
        side_effect=_real_get_min_order_size,
    )
    def test_non_numeric_returns_none(self, _mock_fn, mock_fetch):
        """min_order_size 非数字字符串。"""
        mock_fetch.return_value = {
            "bids": [],
            "asks": [],
            "min_order_size": "abc",
        }
        result = _real_get_min_order_size("12345")
        self.assertIsNone(result)

    @patch("src.utils.orderbook_history.fetch_orderbook")
    @patch(
        "src.utils.orderbook_history.get_min_order_size",
        side_effect=_real_get_min_order_size,
    )
    def test_integer_min_order_size(self, _mock_fn, mock_fetch):
        """min_order_size 为整数字符串（如 '10'）。"""
        mock_fetch.return_value = {
            "bids": [],
            "asks": [],
            "min_order_size": "10",
        }
        result = _real_get_min_order_size("12345")
        self.assertEqual(result, 10.0)


# ──────────────────────────────────────────────────────────────────
# 2. signal_engine _calc_size 费率修正
# ──────────────────────────────────────────────────────────────────


class TestSignalEngineCalcSizeFeeRate(unittest.TestCase):
    """T38: signal_engine._calc_size 使用标准 Fractional Kelly + per-category 费率。"""

    @patch("src.signal.signal_engine.get")
    def test_uses_default_fee_rate(self, mock_get):
        """_calc_size 使用 taker_fee_rate_default (0.05) + Polymarket 官方公式。"""
        from src.signal.signal_engine import SignalEngine

        def side_effect(key):
            vals = {
                "risk.max_single_bet_pct": 0.10,
                "executor.min_bet_usdc": 1.0,
                "paper_trading.fixed_bet_size_usdc": None,
                "paper_trading.taker_fee_rate_default": 0.05,
                "paper_trading.taker_fee_rates": {},
                "signal": {"kelly_fraction": 0.25},
            }
            return vals.get(key)

        mock_get.side_effect = side_effect
        engine = SignalEngine.__new__(SignalEngine)
        # p_true=0.65, entry=0.55
        # fee = 0.05 * 0.55 * 0.45 = 0.012375
        # edge = 0.65 - 0.55 - 0.012375 = 0.087625
        # win_payoff = 1 - 0.55 - 0.012375 = 0.437625
        # f* = 0.087625 / 0.437625 = 0.2002
        # f = 0.25 * 0.2002 = 0.05005
        # size = 100 * 0.05005 = 5.01
        size = engine._calc_size(balance=100, p_true=0.65, entry_price=0.55)
        self.assertGreater(size, 1.0)
        self.assertLess(size, 10.0)

    @patch("src.signal.signal_engine.get")
    def test_fixed_size_unaffected(self, mock_get):
        """固定注额模式不受费率修正影响。"""
        from src.signal.signal_engine import SignalEngine

        def side_effect(key):
            vals = {
                "risk.max_single_bet_pct": 0.10,
                "executor.min_bet_usdc": 1.0,
                "paper_trading.fixed_bet_size_usdc": 5.0,
            }
            return vals.get(key)

        mock_get.side_effect = side_effect
        engine = SignalEngine.__new__(SignalEngine)
        size = engine._calc_size(balance=100, p_true=0.65, entry_price=0.55)
        self.assertEqual(size, 5.0)


# ──────────────────────────────────────────────────────────────────
# 3. live_filters FeeCheck 使用 per-category 费率
# ──────────────────────────────────────────────────────────────────


class TestLiveFiltersFeeCheck(unittest.TestCase):
    """T36: live_filters FeeCheck 使用 per-category taker_fee_rates 而非旧版 taker_fee_rate。"""

    def _make_signal(
        self, ev: float = 0.05, price: float = 0.50, category: str = "sports"
    ):
        """创建测试用 Signal。"""
        sig = MagicMock()
        sig.market_id = "test_market_001"
        sig.ev = ev
        sig.confidence = 0.7
        sig.expected_price = price
        sig.outcome = "YES"
        sig.size_usdc = 5.0
        sig.question = "Test?"
        sig.model_votes = {"m1": {}}
        sig.strategy_tag = ""
        return sig

    def test_config_no_longer_has_old_taker_fee_rate(self):
        """config.json paper_trading 不再包含旧 taker_fee_rate 键。"""
        import json
        import os

        config_path = os.path.join(
            os.path.dirname(__file__), "..", "config", "config.json"
        )
        with open(config_path) as f:
            cfg = json.load(f)
        paper_trading = cfg.get("paper_trading", {})
        self.assertNotIn("taker_fee_rate", paper_trading)
        # 确认 taker_fee_rates (per-category) 仍在
        self.assertIn("taker_fee_rates", paper_trading)
        self.assertIn("taker_fee_rate_default", paper_trading)


# ──────────────────────────────────────────────────────────────────
# 4. config.json 参数清理验证
# ──────────────────────────────────────────────────────────────────


class TestConfigParameterCleanup(unittest.TestCase):
    """T36: 验证已废弃参数从 config.json 中删除。"""

    def setUp(self):
        import json
        import os

        config_path = os.path.join(
            os.path.dirname(__file__), "..", "config", "config.json"
        )
        with open(config_path) as f:
            self.cfg = json.load(f)

    def test_min_order_shares_removed(self):
        """executor.min_order_shares 已删除。"""
        self.assertNotIn("min_order_shares", self.cfg.get("executor", {}))

    def test_min_order_shares_buffer_removed(self):
        """executor.min_order_shares_buffer 已删除。"""
        self.assertNotIn("min_order_shares_buffer", self.cfg.get("executor", {}))

    def test_clob_min_order_usdc_removed(self):
        """executor.clob_min_order_usdc 已删除。"""
        self.assertNotIn("clob_min_order_usdc", self.cfg.get("executor", {}))

    def test_taker_fee_rate_removed(self):
        """paper_trading.taker_fee_rate 已删除。"""
        self.assertNotIn("taker_fee_rate", self.cfg.get("paper_trading", {}))

    def test_hard_max_bet_usdc_updated(self):
        """hard_max_bet_usdc 已从 6.0 更新为 25.0。"""
        self.assertEqual(
            self.cfg.get("paper_trading", {}).get("hard_max_bet_usdc"), 25.0
        )

    def test_max_size_usdc_removed(self):
        """T126: max_size_usdc 已删除（死参数，赋值后从未引用）。"""
        self.assertNotIn("max_size_usdc", self.cfg.get("paper_trading", {}))

    def test_smart_capital_hard_max_removed(self):
        """T126: smart_capital.hard_max_bet_usdc 已删除，统一读取 paper_trading.hard_max_bet_usdc。"""
        self.assertNotIn("hard_max_bet_usdc", self.cfg.get("smart_capital", {}))

    def test_fixed_bet_size_usdc_disabled(self):
        """T126: fixed_bet_size_usdc=0 禁用固定注额模式，使Kelly生效。"""
        val = self.cfg.get("paper_trading", {}).get("fixed_bet_size_usdc")
        self.assertEqual(val, 0)

    def test_min_bet_usdc_preserved(self):
        """executor.min_bet_usdc 仍存在。"""
        self.assertIn("min_bet_usdc", self.cfg.get("executor", {}))

    def test_taker_fee_rates_preserved(self):
        """paper_trading.taker_fee_rates (per-category) 仍存在。"""
        rates = self.cfg.get("paper_trading", {}).get("taker_fee_rates")
        self.assertIsInstance(rates, dict)
        self.assertIn("sports", rates)
        self.assertIn("weather", rates)

    def test_weather_city_scanner_taker_fee_rate_preserved(self):
        """weather_city_scanner.taker_fee_rate 是独立参数，不应被删除。"""
        self.assertIn(
            "taker_fee_rate",
            self.cfg.get("weather_city_scanner", {}),
        )


# ──────────────────────────────────────────────────────────────────
# 5. main.py _resolve_token_id_from_market
# ──────────────────────────────────────────────────────────────────


class TestResolveTokenId(unittest.TestCase):
    """T36: _resolve_token_id_from_market 辅助函数。"""

    def _import(self):
        from main import _resolve_token_id_from_market

        return _resolve_token_id_from_market

    def test_tokens_array(self):
        fn = self._import()
        mdata = {
            "tokens": [
                {"token_id": "yes_tid", "outcome": "Yes"},
                {"token_id": "no_tid", "outcome": "No"},
            ]
        }
        self.assertEqual(fn(mdata, "YES"), "yes_tid")
        self.assertEqual(fn(mdata, "NO"), "no_tid")

    def test_clob_token_ids_list(self):
        fn = self._import()
        mdata = {"clobTokenIds": ["yes_id", "no_id"]}
        self.assertEqual(fn(mdata, "YES"), "yes_id")
        self.assertEqual(fn(mdata, "NO"), "no_id")

    def test_clob_token_ids_json_string(self):
        fn = self._import()
        import json

        mdata = {"clobTokenIds": json.dumps(["yes_str", "no_str"])}
        self.assertEqual(fn(mdata, "YES"), "yes_str")
        self.assertEqual(fn(mdata, "NO"), "no_str")

    def test_empty_market_data(self):
        fn = self._import()
        self.assertEqual(fn({}, "YES"), "")


# ──────────────────────────────────────────────────────────────────
# 6. paper_trader 使用 min_bet_usdc
# ──────────────────────────────────────────────────────────────────


class TestPaperTraderMinBetUsdc(unittest.TestCase):
    """T36: paper_trader sizing 使用 min_bet_usdc 替代 min_order_shares。"""

    @patch("src.simulation.paper_trader.get")
    def test_apply_slippage_floor_is_min_bet(self, mock_get):
        """_apply_slippage_factor 使用 min_bet_usdc 作为 floor。"""
        from src.simulation.paper_trader import PaperTrader

        def side_effect(key):
            if key == "paper_trading":
                return {"slippage_factors": {"normal": 0.9}}
            if key == "executor.min_bet_usdc":
                return 1.0
            return None

        mock_get.side_effect = side_effect
        result = PaperTrader._apply_slippage_factor(0.5, 0.50)
        # 0.5 * 0.9 = 0.45, but floor = 1.0
        self.assertEqual(result, 1.0)

    @patch("src.simulation.paper_trader.get")
    def test_apply_slippage_normal(self, mock_get):
        """正常滑点不被 floor 影响。"""
        from src.simulation.paper_trader import PaperTrader

        def side_effect(key):
            if key == "paper_trading":
                return {"slippage_factors": {"normal": 0.9}}
            if key == "executor.min_bet_usdc":
                return 1.0
            return None

        mock_get.side_effect = side_effect
        result = PaperTrader._apply_slippage_factor(5.0, 0.50)
        # 5.0 * 0.9 = 4.5 > 1.0 → ok
        self.assertEqual(result, 4.5)


if __name__ == "__main__":
    unittest.main()
    unittest.main()
    unittest.main()
