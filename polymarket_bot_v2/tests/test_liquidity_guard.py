"""
test_liquidity_guard.py — 流动性真空止损防护测试

覆盖：
  - 流动性健康时止损被门控（fair_ref > trigger_price）
  - 多周期确认（armed_ticks_required）
  - 地板止损跳过门控
  - 价差过大时不信任 fair_ref
  - _compute_fair_ref / _compute_spread 辅助方法
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
    market_id: str = "mkt-liq-1",
    entry_price: float = 0.50,
    outcome: str = "YES",
    end_date: str = "2027-12-31T12:00:00Z",
    category: str = "anchor_test",
) -> dict:
    return {
        "bet_id": str(uuid.uuid4()),
        "market_id": market_id,
        "question": "Will something happen?",
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
    market_prices: {market_id: {"bestBid": x, "bestAsk": y}}
    """
    tracker = OutcomeTracker()

    def fake_fetch(market_ids, slug_map=None):
        result = {}
        for mid in market_ids:
            p = market_prices.get(mid, {})
            bid = p.get("bestBid", 0.5)
            ask = p.get("bestAsk", bid + 0.01)
            result[mid] = {
                "id": mid,
                "resolved": False,
                "bestBid": bid,
                "bestAsk": ask,
                "tokens": [
                    {"outcome": "Yes", "price": bid},
                    {"outcome": "No", "price": round(1.0 - ask, 4)},
                ],
            }
        return result

    tracker._fetch_market_statuses = fake_fetch
    return tracker


# ---------------------------------------------------------------------------
# _compute_fair_ref
# ---------------------------------------------------------------------------


class TestComputeFairRef:
    def test_yes_mid_price(self):
        mdata = {"bestBid": 0.40, "bestAsk": 0.50}
        fair = OutcomeTracker._compute_fair_ref(mdata, "YES")
        assert fair == pytest.approx(0.45, abs=0.001)

    def test_no_mid_price(self):
        mdata = {"bestBid": 0.40, "bestAsk": 0.50}
        fair = OutcomeTracker._compute_fair_ref(mdata, "NO")
        # NO fair = 1 - 0.45 = 0.55
        assert fair == pytest.approx(0.55, abs=0.001)

    def test_missing_bid_returns_none(self):
        mdata = {"bestAsk": 0.50}
        assert OutcomeTracker._compute_fair_ref(mdata, "YES") is None

    def test_missing_ask_returns_none(self):
        mdata = {"bestBid": 0.40}
        assert OutcomeTracker._compute_fair_ref(mdata, "YES") is None


# ---------------------------------------------------------------------------
# _compute_spread
# ---------------------------------------------------------------------------


class TestComputeSpread:
    def test_normal_spread(self):
        mdata = {"bestBid": 0.49, "bestAsk": 0.51}
        spread = OutcomeTracker._compute_spread(mdata)
        # (0.51 - 0.49) / 0.50 = 0.04
        assert spread == pytest.approx(0.04, abs=0.001)

    def test_wide_spread(self):
        mdata = {"bestBid": 0.30, "bestAsk": 0.50}
        spread = OutcomeTracker._compute_spread(mdata)
        # (0.50 - 0.30) / 0.40 = 0.50
        assert spread == pytest.approx(0.50, abs=0.01)

    def test_missing_data_returns_none(self):
        assert OutcomeTracker._compute_spread({"bestBid": 0}) is None
        assert OutcomeTracker._compute_spread({}) is None


# ---------------------------------------------------------------------------
# 流动性门控集成测试
# ---------------------------------------------------------------------------

_LIQ_CFG = {
    "enabled": True,
    "floor_price": 0.07,
    "standard_stop_pct": 0.28,
    "weather_standard_stop_pct": 0.22,
    "reversal_exit_price": 0.35,
    "expiry_stop_hours": 8,
    "expiry_stop_loss_pct": 0.25,
    "weather_edge_floor_price": 0.03,
    "early_stop": {"enabled": False},
    "stoploss_mode": {
        "default": "market",
        "per_category": {"anchor_test": "anchor"},
    },
    "liquidity_guard": {
        "enabled": True,
        "armed_ticks_required": 2,
        "fair_ref_spread_max": 0.15,
    },
}


class TestLiquidityGuardIntegration:
    """集成测试：止损 + 流动性真空防护。"""

    def test_healthy_market_blocks_stop_loss(self):
        """
        bestBid 触发标准止损阈值，但 mid-price 健康 → 不止损。
        入场 0.50，bestBid=0.34（-32%触发28%止损），
        但 bestAsk=0.50 → mid=0.42 > trigger=0.36 → 市场健康，跳过。
        """
        bet = _make_bet(entry_price=0.50)
        # bestBid 很低但 bestAsk 正常 = 流动性真空
        tracker = _make_tracker({"mkt-liq-1": {"bestBid": 0.34, "bestAsk": 0.50}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                _LIQ_CFG if k == "stop_loss" else d
            )
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 0, "Healthy fair_ref should block stop-loss on first tick"

    def test_multi_tick_confirmation(self):
        """连续 2 tick 都触发 → 第二次确认止损。"""
        bet = _make_bet(entry_price=0.50)
        # bestBid=0.34, bestAsk=0.36 → mid=0.35, trigger=0.36
        # spread = (0.36-0.34)/0.35 = 0.057 < 0.15 → spread 正常
        # mid=0.35 < trigger=0.36 → 不健康 → 进入 armed
        tracker = _make_tracker({"mkt-liq-1": {"bestBid": 0.34, "bestAsk": 0.36}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                _LIQ_CFG if k == "stop_loss" else d
            )
            # Tick 1: armed=1/2, no stop
            stops1 = tracker.check_stop_loss_exits([bet])
            assert len(stops1) == 0
            assert tracker._armed_ticks.get("mkt-liq-1") == 1

            # Tick 2: armed=2/2, triggers stop
            stops2 = tracker.check_stop_loss_exits([bet])
            assert len(stops2) == 1
            assert stops2[0]["trigger"] == "standard_sl"

    def test_floor_stop_bypasses_guard(self):
        """地板止损 (price < 0.07) 直接触发，不走流动性门控。"""
        bet = _make_bet(entry_price=0.50)
        tracker = _make_tracker({"mkt-liq-1": {"bestBid": 0.05, "bestAsk": 0.06}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                _LIQ_CFG if k == "stop_loss" else d
            )
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 1
        assert stops[0]["trigger"] == "floor"

    def test_wide_spread_still_triggers(self):
        """
        bid-ask 价差很大 (>15%) → fair_ref 不可信 → 继续 armed。
        入场 0.50, bestBid=0.34, bestAsk=0.55 → mid=0.445
        spread = (0.55-0.34)/0.445 = 0.47 > 0.15 → 大价差
        fair_ref=0.445 > trigger=0.36 但 spread 可疑 → 不信任 fair_ref
        """
        bet = _make_bet(entry_price=0.50)
        tracker = _make_tracker({"mkt-liq-1": {"bestBid": 0.34, "bestAsk": 0.55}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                _LIQ_CFG if k == "stop_loss" else d
            )
            # Tick 1: wide spread → armed but not triggered
            stops1 = tracker.check_stop_loss_exits([bet])
            assert len(stops1) == 0
            assert tracker._armed_ticks.get("mkt-liq-1") == 1

            # Tick 2: armed=2 → confirmed
            stops2 = tracker.check_stop_loss_exits([bet])
            assert len(stops2) == 1

    def test_recovery_resets_armed(self):
        """市场恢复 → armed 计数重置。"""
        bet = _make_bet(entry_price=0.50)

        # Tick 1: 触发 armed (bad price, tight spread)
        tracker = _make_tracker({"mkt-liq-1": {"bestBid": 0.34, "bestAsk": 0.36}})
        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                _LIQ_CFG if k == "stop_loss" else d
            )
            tracker.check_stop_loss_exits([bet])
            assert tracker._armed_ticks.get("mkt-liq-1") == 1

        # Tick 2: 市场恢复 (bestBid 回升)
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-liq-1": {"bestBid": 0.48, "bestAsk": 0.50}}
        )._fetch_market_statuses
        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                _LIQ_CFG if k == "stop_loss" else d
            )
            stops = tracker.check_stop_loss_exits([bet])
            assert len(stops) == 0
            assert "mkt-liq-1" not in tracker._armed_ticks

    def test_disabled_guard_triggers_immediately(self):
        """liquidity_guard disabled → 第一次就触发。"""
        disabled_cfg = dict(_LIQ_CFG)
        disabled_cfg["liquidity_guard"] = {"enabled": False}

        bet = _make_bet(entry_price=0.50)
        tracker = _make_tracker({"mkt-liq-1": {"bestBid": 0.34, "bestAsk": 0.50}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                disabled_cfg if k == "stop_loss" else d
            )
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"


# ---------------------------------------------------------------------------
# Anchor Price 防闪跌测试
# ---------------------------------------------------------------------------

_ANCHOR_CFG = {
    "enabled": True,
    "floor_price": 0.07,
    "standard_stop_pct": 0.28,
    "weather_standard_stop_pct": 0.22,
    "reversal_exit_price": 0.35,
    "expiry_stop_hours": 8,
    "expiry_stop_loss_pct": 0.25,
    "weather_edge_floor_price": 0.03,
    "early_stop": {"enabled": False},
    "stoploss_mode": {
        "default": "market",
        "per_category": {"anchor_test": "anchor"},
    },
    "liquidity_guard": {
        "enabled": True,
        "armed_ticks_required": 1,
        "fair_ref_spread_max": 0.15,
        "anchor_price_enabled": True,
        "anchor_weight": 0.6,
        "anchor_max_loss_override": 0.50,
    },
}


class TestAnchorPriceProtection:
    """锚定价防闪跌保护测试（来源：@c_dylanber 价格真空防护策略）。"""

    def test_anchor_blocks_false_stop(self):
        """
        bestBid闪跌触发止损，但 anchor_price（入场价加权）仍在安全区间 → 不止损。
        入场0.50, bestBid=0.34(-32% > 28%触发)，bestAsk=0.56 → mid=0.45
        anchor = 0.6*0.50 + 0.4*0.45 = 0.48
        anchor_return = (0.48-0.50)/0.50 = -0.04 > -0.28 → 安全，跳过
        """
        bet = _make_bet(entry_price=0.50)
        tracker = _make_tracker({"mkt-liq-1": {"bestBid": 0.34, "bestAsk": 0.56}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                _ANCHOR_CFG if k == "stop_loss" else d
            )
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 0

    def test_anchor_allows_real_stop(self):
        """
        bestBid 和 mid-price 都跌破止损阈值 → anchor 也不安全 → 止损触发。
        入场0.50, bestBid=0.20, bestAsk=0.25 → mid=0.225
        anchor = 0.6*0.50 + 0.4*0.225 = 0.39
        anchor_return = (0.39-0.50)/0.50 = -0.22 < -0.22? borderline
        trigger_price = 0.50*(1-0.28)=0.36, anchor=0.39>0.36 → 还是安全
        需要 mid 更低才触发。用 bestBid=0.10, bestAsk=0.15 → mid=0.125
        anchor = 0.6*0.50 + 0.4*0.125 = 0.35
        anchor_return = (0.35-0.50)/0.50 = -0.30 ≤ -0.28 → 触发
        """
        bet = _make_bet(entry_price=0.50)
        tracker = _make_tracker({"mkt-liq-1": {"bestBid": 0.10, "bestAsk": 0.15}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                _ANCHOR_CFG if k == "stop_loss" else d
            )
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"

    def test_anchor_disabled_falls_through(self):
        """anchor_price_enabled=false → 回退到原有 fair_ref 门控逻辑。"""
        cfg = dict(_ANCHOR_CFG)
        cfg["liquidity_guard"] = dict(cfg["liquidity_guard"])
        cfg["liquidity_guard"]["anchor_price_enabled"] = False
        cfg["liquidity_guard"]["armed_ticks_required"] = 2

        bet = _make_bet(entry_price=0.50)
        # bestBid触发止损，但mid健康且spread窄 → fair_ref门控生效→不止损
        tracker = _make_tracker({"mkt-liq-1": {"bestBid": 0.34, "bestAsk": 0.38}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (cfg if k == "stop_loss" else d)
            stops = tracker.check_stop_loss_exits([bet])

        # mid = (0.34+0.38)/2 = 0.36, trigger = 0.50*0.72 = 0.36
        # mid(0.36) >= trigger(0.36) → fair_ref门控 → spread=(0.38-0.34)/0.36=0.11 < 0.15 → 市场健康
        assert len(stops) == 0

    def test_anchor_with_wide_spread_continues_to_armed(self):
        """
        anchor_price 安全 + spread过大 → 但loss不够深 → anchor保护生效。
        入场0.50, bestBid=0.36, bestAsk=0.70 → spread巨大
        return = (0.36-0.50)/0.50 = -28%, BUT -28% > -50%(anchor_max_loss_override)
        → anchor仍然生效: anchor = 0.6*0.50 + 0.4*0.53 = 0.512
        → anchor_return = (0.512-0.50)/0.50 = +2.4% > -28% → 安全
        """
        bet = _make_bet(entry_price=0.50)
        tracker = _make_tracker({"mkt-liq-1": {"bestBid": 0.36, "bestAsk": 0.70}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (
                _ANCHOR_CFG if k == "stop_loss" else d
            )
            stops = tracker.check_stop_loss_exits([bet])

        # -28% ≤ -28% standard → triggers check
        # BUT anchor = 0.6*0.50 + 0.4*((0.36+0.70)/2) = 0.6*0.50+0.4*0.53 = 0.512
        # anchor_return = +2.4% > -28% AND anchor(0.512) > trigger(0.36) → 安全
        assert len(stops) == 0


class TestAnchorMaxLossOverride:
    """Phase29: 深度亏损时强制绕过AnchorPrice保护 — 修复crypto(-62%)/weather(-49%)无法止损的BUG。"""

    def test_deep_loss_bypasses_anchor(self):
        """
        入场0.57, bestBid=0.24, bestAsk=0.28 → return=-57.9%
        正常anchor = 0.6*0.57 + 0.4*0.26 = 0.446 → return=-21.8% → "锚定安全"
        但 return_pct (-57.9%) < -anchor_max_loss_override (-30%) → 绕过anchor
        → 进入armed确认 → armed_ticks=1=required(1) → 止损触发
        """
        cfg = dict(_ANCHOR_CFG)
        cfg["standard_stop_pct"] = 0.30
        cfg["liquidity_guard"] = dict(cfg["liquidity_guard"])
        cfg["liquidity_guard"]["anchor_max_loss_override"] = 0.30
        cfg["liquidity_guard"]["armed_ticks_required"] = 1

        bet = _make_bet(entry_price=0.57, market_id="mkt-crypto-deep")
        tracker = _make_tracker({"mkt-crypto-deep": {"bestBid": 0.24, "bestAsk": 0.28}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (cfg if k == "stop_loss" else d)
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"

    def test_moderate_loss_still_protected_by_anchor(self):
        """
        入场0.50, bestBid=0.39, bestAsk=0.42 → return=-22%
        return_pct (-22%) > -anchor_max_loss_override (-30%) → anchor仍生效
        anchor = 0.6*0.50 + 0.4*0.405 = 0.462, return=-7.6% > -30% → 安全
        """
        cfg = dict(_ANCHOR_CFG)
        cfg["standard_stop_pct"] = 0.30
        cfg["liquidity_guard"] = dict(cfg["liquidity_guard"])
        cfg["liquidity_guard"]["anchor_max_loss_override"] = 0.30
        cfg["liquidity_guard"]["armed_ticks_required"] = 1

        bet = _make_bet(entry_price=0.50, market_id="mkt-moderate")
        tracker = _make_tracker({"mkt-moderate": {"bestBid": 0.39, "bestAsk": 0.42}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (cfg if k == "stop_loss" else d)
            stops = tracker.check_stop_loss_exits([bet])

        # -22% loss but anchor says safe → no stop
        assert len(stops) == 0

    def test_weather_deep_loss_bypasses_anchor(self):
        """
        天气NO仓位: 入场0.56, 现在YES_ask=0.72 → NO sell = 1-0.72 = 0.28
        return_pct = (0.28-0.56)/0.56 = -50% < -30% → 绕过anchor → 止损
        """
        cfg = dict(_ANCHOR_CFG)
        cfg["standard_stop_pct"] = 0.25
        cfg["weather_standard_stop_pct"] = 0.25
        cfg["liquidity_guard"] = dict(cfg["liquidity_guard"])
        cfg["liquidity_guard"]["anchor_max_loss_override"] = 0.30
        cfg["liquidity_guard"]["armed_ticks_required"] = 1

        bet = _make_bet(
            entry_price=0.56,
            market_id="mkt-weather-deep",
            outcome="NO",
            category="weather",
        )
        # YES book: bestBid=0.70, bestAsk=0.72 → NO sell = 1-0.72 = 0.28
        tracker = _make_tracker(
            {"mkt-weather-deep": {"bestBid": 0.70, "bestAsk": 0.72}}
        )

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (cfg if k == "stop_loss" else d)
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"

    def test_exactly_at_override_threshold_bypasses(self):
        """
        return_pct exactly -30% → bypass anchor (≤ condition)
        入场0.50, bestBid=0.35 → return=-30%
        """
        cfg = dict(_ANCHOR_CFG)
        cfg["standard_stop_pct"] = 0.28
        cfg["liquidity_guard"] = dict(cfg["liquidity_guard"])
        cfg["liquidity_guard"]["anchor_max_loss_override"] = 0.30
        cfg["liquidity_guard"]["armed_ticks_required"] = 1

        bet = _make_bet(entry_price=0.50, market_id="mkt-exact")
        tracker = _make_tracker({"mkt-exact": {"bestBid": 0.35, "bestAsk": 0.37}})

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.side_effect = lambda k, d=None: (cfg if k == "stop_loss" else d)
            stops = tracker.check_stop_loss_exits([bet])

        assert len(stops) == 1
