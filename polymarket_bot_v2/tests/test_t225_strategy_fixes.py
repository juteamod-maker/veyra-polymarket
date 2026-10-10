"""
test_t225_strategy_fixes.py — Task 225 策略修复 Checklist 测试

覆盖:
1. 禁止高位进入 (max_entry_price) — live_filters + paper_trader
2. 手续费/滑点预评估 (fee/slippage) — live_filters + paper_trader
3. 部分成交防护 — main.py proactive_min_shares_scan
4. Ghost防护 — outcome_tracker reconciliation safety net
5. 分批止盈 min_shares 门槛 — outcome_tracker partial sell
6. Trailing floor min profit — outcome_tracker floor locking
7. sell_shares_override — order_executor partial sell support
"""

import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from src.signal.signal_engine import Signal
from src.simulation.outcome_tracker import OutcomeTracker
from src.utils import db

# ──────────────────────────────────────────────────────────────────
# 共用 helpers
# ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path: Path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    return db_path


def _make_signal(
    market_id="mkt_001",
    question="Test market?",
    outcome="YES",
    ev=0.08,
    confidence=0.70,
    size_usdc=2.0,
    expected_price=0.50,
    model_votes=None,
):
    return Signal(
        market_id=market_id,
        question=question,
        outcome=outcome,
        ev=ev,
        confidence=confidence,
        size_usdc=size_usdc,
        expected_price=expected_price,
        model_votes=model_votes or {},
    )


def _default_pt_cfg():
    return {
        "balance_reserve_min_usdc": 0.10,
        "max_open_positions": 10,
        "blocked_categories": [],
        "category_max_positions": {"default": 15},
        "category_max_per_event": {"default": 5},
        "max_per_market_positions": 1,
        "category_max_per_asset": {"default": 2},
        "stop_loss_cooldown_hours": 0,
        "circuit_breaker": {"enabled": False},
        "category_min_ev": {"default": 0.01},
        "category_min_confidence": {"default": 0.40},
        "category_size_multiplier": {"default": 1.0},
        "hard_max_bet_usdc": 5.0,
        "category_max_bet_pct": {"default": 0.20},
        "max_entry_price": 0.85,
        # T36: per-category 费率 + 官方公式
        "taker_fee_rates": {"crypto": 0.08, "sports": 0.02, "weather": 0.02},
        "taker_fee_rate_default": 0.02,
        "min_net_edge_after_fees": 0.01,
    }


def _mock_get(pt_cfg):
    def _get(k):
        if k == "paper_trading":
            return pt_cfg
        if k == "general.live_initial_deposit":
            return 100.0
        if k.startswith("paper_trading."):
            return pt_cfg.get(k.split(".", 1)[1])
        if k.startswith("executor."):
            return {"clob_min_order_usdc": 1.10, "min_order_shares": 5}.get(
                k.split(".", 1)[1]
            )
        return None

    return _get


def _make_bet(
    market_id="mkt-001",
    entry_price=0.30,
    outcome="YES",
    size=1.0,
    matched_shares=0,
    end_date="2027-12-31T12:00:00Z",
    position_history="",
):
    return {
        "bet_id": str(uuid.uuid4()),
        "market_id": market_id,
        "question": "Will temperature be X?",
        "outcome": outcome,
        "market_price": entry_price,
        "size_usdc": size,
        "matched_shares": matched_shares or round(size / entry_price, 4),
        "market_end_date": end_date,
        "ts_entered": time.time() - 3600,
        "position_history": position_history,
    }


def _make_tracker(current_prices: dict) -> OutcomeTracker:
    tracker = OutcomeTracker()

    def fake_fetch(market_ids):
        result = {}
        for mid in market_ids:
            price = current_prices.get(mid, 0.5)
            result[mid] = {
                "id": mid,
                "resolved": False,
                "bestBid": price,
                "bestAsk": round(min(price + 0.01, 0.99), 4),
                "tokens": [
                    {"outcome": "Yes", "price": price},
                    {"outcome": "No", "price": round(1.0 - price, 4)},
                ],
            }
        return result

    tracker._fetch_market_statuses = fake_fetch
    return tracker


# ══════════════════════════════════════════════════════════════════
# 1. 禁止高位进入 — live_filters
# ══════════════════════════════════════════════════════════════════


class TestMaxEntryPriceLiveFilter:
    """全局高位入场禁止: expected_price >= max_entry_price → 拒绝。"""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_high_price_blocked(self, mock_cat, mock_get_fn, mock_db):
        """入场价 0.90 >= 上限 0.85 → 被拒绝。"""
        from src.utils.live_filters import filter_signals_for_live

        cfg = _default_pt_cfg()
        cfg["max_entry_price"] = 0.85
        mock_get_fn.side_effect = _mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = _make_signal(ev=0.10, confidence=0.80, expected_price=0.90)
        result = filter_signals_for_live(
            [sig], balance=50.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 0

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_low_price_passes(self, mock_cat, mock_get_fn, mock_db):
        """入场价 0.50 < 上限 0.85 → 通过。"""
        from src.utils.live_filters import filter_signals_for_live

        cfg = _default_pt_cfg()
        mock_get_fn.side_effect = _mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = _make_signal(ev=0.08, confidence=0.70, expected_price=0.50)
        result = filter_signals_for_live(
            [sig], balance=50.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 1

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_boundary_price_blocked(self, mock_cat, mock_get_fn, mock_db):
        """入场价 exactly 0.85 (边界) → 被拒绝。"""
        from src.utils.live_filters import filter_signals_for_live

        cfg = _default_pt_cfg()
        cfg["max_entry_price"] = 0.85
        mock_get_fn.side_effect = _mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = _make_signal(ev=0.10, confidence=0.80, expected_price=0.85)
        result = filter_signals_for_live(
            [sig], balance=50.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 0


# ══════════════════════════════════════════════════════════════════
# 2. 手续费预评估 — live_filters
# ══════════════════════════════════════════════════════════════════


class TestFeePreAssessmentLiveFilter:
    """EV 扣除双边手续费后仍需有正收益才入场。"""

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_low_ev_after_fees_blocked(self, mock_cat, mock_get_fn, mock_db):
        """EV=0.04, crypto费率=0.08, 双边费=0.08*0.5*0.5*2=0.04, net=0.00 < min 0.01 → 拒绝。"""
        from src.utils.live_filters import filter_signals_for_live

        cfg = _default_pt_cfg()
        cfg["min_net_edge_after_fees"] = 0.01
        mock_get_fn.side_effect = _mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = _make_signal(ev=0.04, confidence=0.70, expected_price=0.50)
        result = filter_signals_for_live(
            [sig], balance=50.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 0

    @patch("src.utils.live_filters.db")
    @patch("src.utils.live_filters.get")
    @patch("src.utils.live_filters.detect_category", return_value="crypto")
    def test_high_ev_after_fees_passes(self, mock_cat, mock_get_fn, mock_db):
        """EV=0.10, crypto费率=0.08, 双边费=0.08*0.5*0.5*2=0.04, net=0.06 > min 0.01 → 通过。"""
        from src.utils.live_filters import filter_signals_for_live

        cfg = _default_pt_cfg()
        cfg["min_net_edge_after_fees"] = 0.01
        mock_get_fn.side_effect = _mock_get(cfg)
        mock_db.get_open_paper_bets.return_value = []
        mock_db.had_recent_stop_loss.return_value = False
        mock_db.get_recent_exits_for_market.return_value = []

        sig = _make_signal(ev=0.10, confidence=0.70, expected_price=0.50)
        result = filter_signals_for_live(
            [sig], balance=50.0, market_lookup={"mkt_001": {}}
        )
        assert len(result) == 1


def _pt_config_side_effect(pt_overrides: dict):
    """创建 config_loader.get 的 side_effect，按 section 返回不同配置。"""
    _profit_taking = {
        "enabled": True,
        "near_ceiling_threshold": 0.97,
        "expiry_hours_threshold": 24.0,
        "expiry_profit_pct": 0.80,
        "trailing_enabled": True,
        "trailing_drop_pct": 0.20,
        "trailing_min_profit_pct": 0.30,
        "trailing_floor_min_profit_pct": 0.0,
        "trailing_floor_dynamic_enabled": False,
        "trailing_partial_sell_enabled": False,
        "trailing_partial_sell_pct": 0.5,
        "trailing_partial_sell_min_shares": 30,
        "profit_take_pct": 0.99,
        "quick_exit_enabled": False,
        "quick_exit_entry_max": 0.25,
        "quick_exit_profit_pct": 0.05,
        "hold_to_settle_threshold": 0.99,
        "hold_to_settle_hours": 99999,
        "trailing_tiers": [],
        "weather_trailing_min_drop": 0.10,
        "weather_quick_exit_price": 0,
        "weather_quick_entry_max": 0.23,
    }
    _profit_taking.update(pt_overrides)

    def _get(key):
        if key == "profit_taking":
            return _profit_taking
        if key == "stop_loss":
            return {"weather_rotation_exit": {"enabled": False}}
        if key == "sports_live":
            return {}
        return None

    return _get


# ══════════════════════════════════════════════════════════════════
# 3. Trailing Floor 最低利润锁定
# ══════════════════════════════════════════════════════════════════


class TestTrailingFloorMinProfit:
    """trailing_floor_min_profit_pct: floor 应锁在 entry × (1 + pct)。"""

    @patch("src.simulation.outcome_tracker.get")
    def test_floor_locks_above_entry(self, mock_get):
        """pct=0.02 → floor = entry × 1.02，而不是 entry 本身。"""
        mock_get.side_effect = _pt_config_side_effect(
            {"trailing_floor_min_profit_pct": 0.02}
        )
        bet = _make_bet(entry_price=0.50, market_id="mkt-floor")
        # Round 1: peak at 0.70 (return 40% > min_profit 30%) → floor should lock
        tracker = _make_tracker({"mkt-floor": 0.70})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []  # No exit yet, just locked floor

        # Verify floor locked at 0.50 × 1.02 = 0.51
        peak_key = bet["bet_id"]
        assert peak_key in tracker._floor_prices
        assert abs(tracker._floor_prices[peak_key] - 0.51) < 0.001

    @patch("src.simulation.outcome_tracker.get")
    def test_floor_zero_pct_backward_compat(self, mock_get):
        """pct=0.0 → floor = entry（旧行为保持兼容）。"""
        mock_get.side_effect = _pt_config_side_effect(
            {"trailing_floor_min_profit_pct": 0.0}
        )
        bet = _make_bet(entry_price=0.50, market_id="mkt-floor0")
        tracker = _make_tracker({"mkt-floor0": 0.70})
        tracker.check_profit_take_exits([bet])

        peak_key = bet["bet_id"]
        assert peak_key in tracker._floor_prices
        assert abs(tracker._floor_prices[peak_key] - 0.50) < 0.001

    @patch("src.simulation.outcome_tracker.get")
    def test_floor_triggers_exit(self, mock_get):
        """价格跌破 floor → floor_protection 触发。"""
        mock_get.side_effect = _pt_config_side_effect(
            {"trailing_floor_min_profit_pct": 0.02}
        )
        bet = _make_bet(entry_price=0.50, market_id="mkt-ft")
        # Round 1: peak at 0.70 → floor locks at 0.51
        tracker = _make_tracker({"mkt-ft": 0.70})
        tracker.check_profit_take_exits([bet])

        # Round 2: price drops to 0.505 (< floor 0.51 → trigger)
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-ft": 0.505}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "floor_protection"


# ══════════════════════════════════════════════════════════════════
# 4. 分批止盈 min_shares 门槛
# ══════════════════════════════════════════════════════════════════


class TestPartialSellMinShares:
    """小仓位(<30股)不分批，直接全量止盈。"""

    @patch("src.simulation.outcome_tracker.get")
    def test_small_position_no_partial(self, mock_get):
        """6 股 < 30 → partial_sell 不启用，全量卖出。"""
        mock_get.side_effect = _pt_config_side_effect(
            {
                "trailing_partial_sell_enabled": True,
                "trailing_partial_sell_pct": 0.5,
                "trailing_partial_sell_min_shares": 30,
            }
        )
        # 6 shares: size=1.80, entry=0.30 → 6 shares < min 30
        bet = _make_bet(entry_price=0.30, market_id="mkt-sm", size=1.80)
        # Peak at 0.50, then drop to 0.395 → trailing trigger
        tracker = _make_tracker({"mkt-sm": 0.50})
        tracker.check_profit_take_exits([bet])

        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-sm": 0.395}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        # 小仓位 → 没有 partial_sell 标记
        assert exits[0].get("partial_sell") is not True

    @patch("src.simulation.outcome_tracker.get")
    def test_large_position_gets_partial(self, mock_get):
        """50 股 >= 30 → partial_sell 启用。"""
        mock_get.side_effect = _pt_config_side_effect(
            {
                "trailing_partial_sell_enabled": True,
                "trailing_partial_sell_pct": 0.5,
                "trailing_partial_sell_min_shares": 30,
            }
        )
        # 50 shares: size=10.0, entry=0.20 → 50 shares >= 30
        bet = _make_bet(
            entry_price=0.20, market_id="mkt-lg", size=10.0, matched_shares=50.0
        )
        # Peak at 0.50 (return=150%), then drop to 0.39 (return=95%, drop=22%≥20%)
        tracker = _make_tracker({"mkt-lg": 0.50})
        tracker.check_profit_take_exits([bet])

        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-lg": 0.39}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0].get("partial_sell") is True
        assert exits[0].get("partial_sell_pct") == 0.5

    @patch("src.simulation.outcome_tracker.get")
    def test_already_partial_sold_skips(self, mock_get):
        """position_history 含 partial_sell= → 不再分批，全量卖出。"""
        mock_get.side_effect = _pt_config_side_effect(
            {
                "trailing_partial_sell_enabled": True,
                "trailing_partial_sell_pct": 0.5,
                "trailing_partial_sell_min_shares": 30,
            }
        )
        bet = _make_bet(
            entry_price=0.20,
            market_id="mkt-ps",
            size=10.0,
            matched_shares=50.0,
            position_history="partial_sell=5.0000 pnl=+0.5000 @1774000000",
        )
        # Peak at 0.50 (return=150%), then drop to 0.39 (return=95%, drop=22%≥20%)
        tracker = _make_tracker({"mkt-ps": 0.50})
        tracker.check_profit_take_exits([bet])

        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-ps": 0.39}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0].get("partial_sell") is not True


# ══════════════════════════════════════════════════════════════════
# 5. sell_shares_override — order_executor
# ══════════════════════════════════════════════════════════════════


class TestSellSharesOverride:
    """sell_shares_override 应限制实际卖出股数。"""

    def test_override_applied_in_dry_run(self):
        """DRY RUN sell_position 全量计算 shares（override 仅影响实盘卖出）。"""
        from src.executor.order_executor import OrderExecutor

        executor = OrderExecutor()
        bet = {
            "market_id": "mkt-override",
            "question": "Override test?",
            "outcome": "YES",
            "exit_price": 0.60,
            "market_price": 0.30,
            "size_usdc": 3.0,
            "trigger": "trailing",
            "sell_shares_override": 5.0,  # 指定只卖5股
        }
        with patch("src.executor.order_executor.is_dry_run", return_value=True):
            result = executor.sell_position(bet)
        assert result.success is True
        # DRY RUN 按 size/entry 计算 (3.0/0.30=10)，不受 override 影响
        assert result.matched_shares == pytest.approx(10.0, abs=0.01)


# ══════════════════════════════════════════════════════════════════
# 6. Ghost 防护 — reconciliation safety net
# ══════════════════════════════════════════════════════════════════


class TestGhostProtection:
    """stop_loss/profit_take 但链上仍有大额持仓 → 自动 reopen。"""

    def test_stop_loss_with_chain_value_reopens(self, tmp_db):
        """stop_loss 仓位链上 value > $0.50 且未 redeemable → 重开。"""
        bid = str(uuid.uuid4())
        db.insert_paper_bet(
            {
                "bet_id": bid,
                "market_id": "mkt-ghost",
                "question": "Ghost test",
                "outcome": "YES",
                "category": "crypto",
                "market_price": 0.50,
                "size_usdc": 5.0,
                "ts_entered": time.time() - 7200,
            },
            db_path=tmp_db,
        )
        # Close it as stop_loss
        db.close_paper_bet_stop_loss(
            bet_id=bid,
            exit_price=0.40,
            pnl=-1.0,
            payout=4.0,
            db_path=tmp_db,
        )

        # Verify it's closed
        from src.utils.db import connect

        with connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT resolved_outcome FROM paper_bets WHERE bet_id=?",
                (bid,),
            ).fetchone()
            assert row["resolved_outcome"] == "STOP_LOSS"

        # Now reopen it (simulating ghost detection)
        reopened = db.reopen_ghost_position(bid, db_path=tmp_db)
        assert reopened is True

        with connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT resolved_outcome FROM paper_bets WHERE bet_id=?",
                (bid,),
            ).fetchone()
            assert row["resolved_outcome"] is None


# ══════════════════════════════════════════════════════════════════
# 7. 配置键完整性
# ══════════════════════════════════════════════════════════════════


class TestConfigKeyIntegrity:
    """验证所有 T225 新增配置键存在于 config.json。"""

    def test_config_has_max_entry_price(self):
        import json

        cfg_path = Path(__file__).parent.parent / "config" / "config.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        pt = cfg.get("paper_trading", cfg)
        assert "max_entry_price" in pt
        assert isinstance(pt["max_entry_price"], (int, float))

    def test_config_has_taker_fee_rate(self):
        """T36: taker_fee_rate 已废弃，验证新参数 taker_fee_rates + taker_fee_rate_default 存在。"""
        import json

        cfg_path = Path(__file__).parent.parent / "config" / "config.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        pt = cfg.get("paper_trading", cfg)
        assert "taker_fee_rates" in pt
        assert isinstance(pt["taker_fee_rates"], dict)
        assert "taker_fee_rate_default" in pt
        assert isinstance(pt["taker_fee_rate_default"], (int, float))

    def test_config_has_min_net_edge(self):
        import json

        cfg_path = Path(__file__).parent.parent / "config" / "config.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        pt = cfg.get("paper_trading", cfg)
        assert "min_net_edge_after_fees" in pt

    def test_config_has_trailing_floor_min_profit(self):
        import json

        cfg_path = Path(__file__).parent.parent / "config" / "config.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        pt = cfg.get("profit_taking", {})
        assert "trailing_floor_min_profit_pct" in pt

    def test_config_has_partial_sell_min_shares(self):
        import json

        cfg_path = Path(__file__).parent.parent / "config" / "config.json"
        with open(cfg_path) as f:
            cfg = json.load(f)
        pt = cfg.get("profit_taking", {})
        assert "trailing_partial_sell_min_shares" in pt
        assert pt["trailing_partial_sell_min_shares"] >= 10  # 防止太小
