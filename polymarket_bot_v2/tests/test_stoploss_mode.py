"""
test_stoploss_mode.py — 止损价格模式测试（Phase 44.2 紧急修复）

验证 stoploss_mode 配置：
  - market 模式：纯盘口价止损，跳过 PriceDeviation 和 LiquidityGuard
  - anchor 模式：保留锚定参考价 + 多周期确认（低流动性赛道）
  - PriceDeviation 不再阻止真实下跌的止损

BUG 根因：
  _extract_sell_price() 的 _validate_orderbook_price() 在盘口价与参考价偏差>25%时，
  用旧参考价替代真实暴跌的盘口价，导致止损逻辑看到虚假的"安全"价格。
"""

import time
import uuid
from unittest.mock import patch

import pytest
from src.simulation.outcome_tracker import OutcomeTracker

# ---------------------------------------------------------------------------
# 辅助方法
# ---------------------------------------------------------------------------


def _make_bet(
    market_id: str = "mkt-mode-1",
    entry_price: float = 0.50,
    outcome: str = "YES",
    category: str = "weather",
    end_date: str = "2027-12-31T12:00:00Z",
) -> dict:
    return {
        "bet_id": str(uuid.uuid4()),
        "market_id": market_id,
        "question": "Will the temperature exceed 30C in Tokyo on March 25?",
        "outcome": outcome,
        "market_price": entry_price,
        "size_usdc": 5.0,
        "market_end_date": end_date,
        "ts_entered": time.time() - 3600,
        "category": category,
        "bet_bucket": "",
    }


def _make_tracker(market_prices: dict) -> OutcomeTracker:
    """
    market_prices: {market_id: {"bestBid": x, "bestAsk": y, "lastTradePrice": z}}
    lastTradePrice 用作参考价（PriceDeviation 校验基准）。
    """
    tracker = OutcomeTracker()

    def fake_fetch(market_ids):
        result = {}
        for mid in market_ids:
            p = market_prices.get(mid, {})
            bid = p.get("bestBid", 0.5)
            ask = p.get("bestAsk", bid + 0.01)
            ltp = p.get("lastTradePrice", None)
            result[mid] = {
                "id": mid,
                "resolved": False,
                "bestBid": bid,
                "bestAsk": ask,
                "lastTradePrice": ltp,
                "tokens": [
                    {"outcome": "Yes", "price": bid},
                    {"outcome": "No", "price": round(1.0 - ask, 4)},
                ],
            }
        return result

    tracker._fetch_market_statuses = fake_fetch
    return tracker


# 基础止损配置（不含 liquidity_guard 和 stoploss_mode，各测试自行配置）
_BASE_SL_CFG = {
    "enabled": True,
    "floor_price": 0.07,
    "standard_stop_pct": 0.30,
    "weather_standard_stop_pct": 0.25,
    "sports_standard_stop_pct": 0.35,
    "reversal_exit_price": 0.15,
    "expiry_stop_hours": 8,
    "expiry_stop_loss_pct": 0.30,
    "weather_edge_floor_price": 0.03,
    "early_stop": {"enabled": False},
}


# ==================================================================
# 1. Market 模式：纯盘口价止损（天气/体育）
# ==================================================================


class TestMarketMode:
    """market 模式跳过 PriceDeviation 和 LiquidityGuard，直接按盘口价止损。"""

    def _cfg(self, **overrides):
        cfg = dict(_BASE_SL_CFG)
        cfg["stoploss_mode"] = {
            "default": "market",
            "per_category": {"crypto": "anchor"},
        }
        cfg["liquidity_guard"] = {
            "enabled": True,
            "armed_ticks_required": 3,
            "fair_ref_spread_max": 0.15,
            "anchor_price_enabled": True,
            "anchor_weight": 0.6,
            "anchor_max_loss_override": 0.30,
        }
        cfg.update(overrides)
        return cfg

    def test_weather_immediate_stop_loss(self):
        """
        天气 market 模式：盘口价暴跌 → 立即止损。
        入场0.50, bestBid=0.30 → return=-40% < -25%(weather_stop) → 止损
        即使 LiquidityGuard 启用、armed_ticks=3，market模式下全部跳过。
        """
        bet = _make_bet(entry_price=0.50, category="weather")
        tracker = _make_tracker(
            {
                "mkt-mode-1": {
                    "bestBid": 0.30,
                    "bestAsk": 0.32,
                    "lastTradePrice": 0.48,  # 参考价远高于盘口 → PriceDeviation 会替换
                }
            }
        )

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                self._cfg() if k == "stop_loss" else d
            )
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"
        # market 模式用 raw bestBid，不被 PriceDeviation 替换
        assert stops[0]["exit_price"] == pytest.approx(0.30, abs=0.01)

    def test_sports_immediate_stop_loss(self):
        """体育 market 模式：盘口暴跌 → 第一次即止损。"""
        bet = _make_bet(entry_price=0.60, category="sports", market_id="mkt-sports-1")
        tracker = _make_tracker(
            {
                "mkt-sports-1": {
                    "bestBid": 0.35,
                    "bestAsk": 0.40,
                    "lastTradePrice": 0.58,
                }
            }
        )

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                self._cfg() if k == "stop_loss" else d
            )
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"
        assert stops[0]["exit_price"] == pytest.approx(0.35, abs=0.01)

    def test_no_position_market_mode_uses_raw_ask(self):
        """
        NO 仓位在 market 模式下也使用原始 bestAsk。
        入场0.60(NO), bestAsk=0.88 → NO sell = 1-0.88 = 0.12
        return = (0.12-0.60)/0.60 = -80% → 止损
        不被 PriceDeviation 替换（即使 ref=0.37 → deviation=138%）
        """
        bet = _make_bet(
            entry_price=0.60,
            outcome="NO",
            category="weather",
            market_id="mkt-no-1",
        )
        tracker = _make_tracker(
            {
                "mkt-no-1": {
                    "bestBid": 0.85,
                    "bestAsk": 0.88,
                    "lastTradePrice": 0.37,  # 旧参考价
                }
            }
        )

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                self._cfg() if k == "stop_loss" else d
            )
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 1
        # NO sell price = 1 - raw_ask = 1 - 0.88 = 0.12
        assert stops[0]["exit_price"] == pytest.approx(0.12, abs=0.01)
        assert stops[0]["return_pct"] < -0.50

    def test_market_mode_skips_armed_ticks(self):
        """market 模式下不需要多周期确认，第一个tick即触发。"""
        cfg = self._cfg()
        cfg["liquidity_guard"]["armed_ticks_required"] = 5  # 高值

        bet = _make_bet(entry_price=0.50, category="weather")
        tracker = _make_tracker({"mkt-mode-1": {"bestBid": 0.30, "bestAsk": 0.32}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (cfg if k == "stop_loss" else d)
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 1
        # 一次即触发，不需要 armed 确认
        assert tracker._armed_ticks.get("mkt-mode-1") is None

    def test_market_mode_no_trigger_when_price_healthy(self):
        """价格正常时不触发止损（无论什么模式）。"""
        bet = _make_bet(entry_price=0.50, category="weather")
        tracker = _make_tracker({"mkt-mode-1": {"bestBid": 0.48, "bestAsk": 0.50}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                self._cfg() if k == "stop_loss" else d
            )
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 0


# ==================================================================
# 2. Anchor 模式：保留锚定保护（crypto 低流动性）
# ==================================================================


class TestAnchorMode:
    """anchor 模式保留 PriceDeviation 校验 + LiquidityGuard 完整门控。"""

    def _cfg(self, **overrides):
        cfg = dict(_BASE_SL_CFG)
        cfg["stoploss_mode"] = {
            "default": "market",
            "per_category": {"crypto": "anchor"},
        }
        cfg["liquidity_guard"] = {
            "enabled": True,
            "armed_ticks_required": 2,
            "fair_ref_spread_max": 0.15,
            "anchor_price_enabled": True,
            "anchor_weight": 0.6,
            "anchor_max_loss_override": 0.20,
        }
        cfg.update(overrides)
        return cfg

    def test_crypto_anchor_blocks_flash_crash(self):
        """
        crypto anchor 模式：盘口闪跌但 anchor 仍安全 → 不止损。
        入场0.50, bestBid=0.34(-32%), bestAsk=0.56
        mid=0.45, anchor = 0.6*0.50 + 0.4*0.45 = 0.48
        anchor_return = (0.48-0.50)/0.50 = -4% > -30% → 安全
        """
        bet = _make_bet(entry_price=0.50, category="crypto", market_id="mkt-crypto-1")
        tracker = _make_tracker({"mkt-crypto-1": {"bestBid": 0.34, "bestAsk": 0.56}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                self._cfg() if k == "stop_loss" else d
            )
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 0

    def test_crypto_deep_loss_bypasses_anchor(self):
        """
        crypto 深度亏损时绕过 anchor 保护。
        入场0.57, bestBid=0.24 → return=-57.9% < -20%(override) → 绕过
        """
        bet = _make_bet(
            entry_price=0.57, category="crypto", market_id="mkt-crypto-deep"
        )
        tracker = _make_tracker({"mkt-crypto-deep": {"bestBid": 0.24, "bestAsk": 0.28}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                self._cfg() if k == "stop_loss" else d
            )
            # Tick 1: armed=1/2
            stops1 = tracker.check_stop_loss_exits([bet])
            assert len(stops1) == 0

            # Tick 2: armed=2/2 → confirmed
            stops2 = tracker.check_stop_loss_exits([bet])
            assert len(stops2) == 1
            assert stops2[0]["trigger"] == "standard_sl"

    def test_crypto_needs_multi_tick(self):
        """
        crypto anchor 模式需要多周期确认。
        bestBid=0.30, bestAsk=0.32 → mid=0.31, trigger=0.35
        mid<trigger → 不健康 → armed 确认
        """
        bet = _make_bet(
            entry_price=0.50, category="crypto", market_id="mkt-crypto-armed"
        )
        tracker = _make_tracker(
            {"mkt-crypto-armed": {"bestBid": 0.30, "bestAsk": 0.32}}
        )

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                self._cfg() if k == "stop_loss" else d
            )
            # Tick 1: armed=1/2
            stops1 = tracker.check_stop_loss_exits([bet])
            assert len(stops1) == 0
            assert tracker._armed_ticks.get("mkt-crypto-armed") == 1

            # Tick 2: armed=2/2 → trigger
            stops2 = tracker.check_stop_loss_exits([bet])
            assert len(stops2) == 1


# ==================================================================
# 3. _extract_sell_price validate 参数
# ==================================================================


class TestExtractSellPriceValidate:
    """验证 _extract_sell_price 的 validate 参数控制 PriceDeviation 校验。"""

    def test_validate_true_replaces_deviated_price(self):
        """validate=True 时，偏差>80%(Phase 53阈值) 的盘口价被参考价替代。"""
        # 先在缓存中放入新鲜的参考价，防止 auto_update 逻辑干扰
        import src.simulation.outcome_tracker as ot_mod

        ot_mod._ref_price_cache["test-market"] = (0.37, __import__("time").monotonic())
        mdata = {
            "id": "test-market",
            "bestBid": 0.05,
            "bestAsk": 0.88,
            "lastTradePrice": 0.37,
            "tokens": [
                {"outcome": "Yes", "price": 0.05},
                {"outcome": "No", "price": 0.12},
            ],
        }
        # YES: bestBid=0.05, ref=0.37, deviation=86% > 80% → 替换为 0.37
        price_yes = OutcomeTracker._extract_sell_price(mdata, "YES", validate=True)
        assert price_yes == pytest.approx(0.37, abs=0.01)

    def test_validate_false_returns_raw_price(self):
        """validate=False 时，直接返回原始盘口价。"""
        mdata = {
            "id": "test-market",
            "bestBid": 0.20,
            "bestAsk": 0.88,
            "lastTradePrice": 0.37,
            "tokens": [
                {"outcome": "Yes", "price": 0.20},
                {"outcome": "No", "price": 0.12},
            ],
        }
        # YES: bestBid=0.20, 不校验 → 直接返回 0.20
        price_yes = OutcomeTracker._extract_sell_price(mdata, "YES", validate=False)
        assert price_yes == pytest.approx(0.20, abs=0.001)

    def test_validate_false_no_position(self):
        """NO 仓位 validate=False 时直接 1-bestAsk。"""
        mdata = {
            "id": "test-no",
            "bestBid": 0.85,
            "bestAsk": 0.88,
            "lastTradePrice": 0.37,
            "tokens": [
                {"outcome": "Yes", "price": 0.85},
                {"outcome": "No", "price": 0.12},
            ],
        }
        # NO: raw_ask=0.88 → 1-0.88 = 0.12
        price_no = OutcomeTracker._extract_sell_price(mdata, "NO", validate=False)
        assert price_no == pytest.approx(0.12, abs=0.001)

    def test_validate_true_no_deviation_passes_through(self):
        """无偏差时 validate=True 也返回原始价格。"""
        mdata = {
            "id": "test-normal",
            "bestBid": 0.48,
            "bestAsk": 0.52,
            "lastTradePrice": 0.49,
            "tokens": [
                {"outcome": "Yes", "price": 0.48},
                {"outcome": "No", "price": 0.48},
            ],
        }
        price = OutcomeTracker._extract_sell_price(mdata, "YES", validate=True)
        assert price == pytest.approx(0.48, abs=0.01)


# ==================================================================
# 4. 真实 BUG 场景复现
# ==================================================================


class TestRealBugReproduction:
    """
    复现报告中的真实 BUG 场景：
    VPS 日志: [PriceDeviation] bestAsk 1436063: orderbook=0.8800 vs ref=0.3700
              (deviation=+138%), 使用参考价替代
    导致 NO 仓位在盘口价 0.12(=1-0.88) 时仍被认为是 0.63(=1-0.37)
    """

    def test_weather_no_position_stuck_at_reference_price(self):
        """
        旧行为（anchor模式+PriceDeviation）：
        NO仓位入场0.60, bestAsk=0.88, ref=0.37
        PriceDeviation: deviation=138% → 用ref(0.37)替代ask(0.88)
        sell_price = 1-0.37 = 0.63 → return=+5% → "锚定安全"！
        但真实 sell_price = 1-0.88 = 0.12 → return=-80% ！

        新行为（market模式）：
        天气赛道用 market 模式 → validate=False → sell_price=1-0.88=0.12
        return=-80% → 立即止损
        """
        cfg = dict(_BASE_SL_CFG)
        cfg["stoploss_mode"] = {
            "default": "market",
            "per_category": {"crypto": "anchor"},
        }
        cfg["liquidity_guard"] = {
            "enabled": True,
            "armed_ticks_required": 3,
            "fair_ref_spread_max": 0.15,
            "anchor_price_enabled": True,
            "anchor_weight": 0.6,
            "anchor_max_loss_override": 0.30,
        }

        bet = _make_bet(
            entry_price=0.60,
            outcome="NO",
            category="weather",
            market_id="mkt-1436063",
        )
        tracker = _make_tracker(
            {
                "mkt-1436063": {
                    "bestBid": 0.85,
                    "bestAsk": 0.88,
                    "lastTradePrice": 0.37,  # 旧参考价
                }
            }
        )

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (cfg if k == "stop_loss" else d)
            stops = tracker.check_stop_loss_exits([bet])

        # 天气=market模式 → 不被 PriceDeviation 替换 → 真实价格0.12 → 止损
        assert len(stops) == 1
        assert stops[0]["exit_price"] == pytest.approx(0.12, abs=0.01)
        assert stops[0]["return_pct"] < -0.50

    def test_sports_deep_loss_triggers_immediately(self):
        """
        体育仓位在盘口暴跌时不需要等多周期确认。
        入场0.50, bestBid=0.20 → return=-60% → market模式立即止损。
        """
        cfg = dict(_BASE_SL_CFG)
        cfg["stoploss_mode"] = {
            "default": "market",
            "per_category": {"crypto": "anchor"},
        }
        cfg["liquidity_guard"] = {
            "enabled": True,
            "armed_ticks_required": 5,
            "fair_ref_spread_max": 0.15,
            "anchor_price_enabled": True,
            "anchor_weight": 0.6,
            "anchor_max_loss_override": 0.30,
        }

        bet = _make_bet(
            entry_price=0.50, category="sports", market_id="mkt-sports-deep"
        )
        tracker = _make_tracker(
            {
                "mkt-sports-deep": {
                    "bestBid": 0.20,
                    "bestAsk": 0.22,
                    "lastTradePrice": 0.47,
                }
            }
        )

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (cfg if k == "stop_loss" else d)
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"
        assert stops[0]["exit_price"] == pytest.approx(0.20, abs=0.01)
