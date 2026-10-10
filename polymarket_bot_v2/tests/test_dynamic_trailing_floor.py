"""T65: Dynamic Trailing Floor 测试

验证动态 floor 随 peak 上升、棘轮只升不降、赛道特定 retreat_pct。
"""

import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
from src.simulation.outcome_tracker import OutcomeTracker

# ── Helpers ──────────────────────────────────────────────────────


def _make_bet(
    market_id="mkt-001",
    entry_price=0.30,
    outcome="YES",
    category="",
    size=1.0,
    matched_shares=0,
    end_date="2027-12-31T12:00:00Z",
):
    return {
        "bet_id": str(uuid.uuid4()),
        "market_id": market_id,
        "question": "Will temperature be X?",
        "outcome": outcome,
        "category": category,
        "market_price": entry_price,
        "size_usdc": size,
        "matched_shares": matched_shares or round(size / entry_price, 4),
        "market_end_date": end_date,
        "ts_entered": time.time() - 3600,
        "position_history": "",
    }


def _make_tracker(current_prices: dict) -> OutcomeTracker:
    tracker = OutcomeTracker()

    def fake_fetch(market_ids, slug_map=None):
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


def _dynamic_pt_config(overrides: dict = None):
    """Config side_effect with dynamic floor enabled by default."""
    _pt = {
        "enabled": True,
        "near_ceiling_threshold": 0.97,
        "expiry_hours_threshold": 24.0,
        "expiry_profit_pct": 0.80,
        "trailing_enabled": True,
        "trailing_drop_pct": 0.45,
        "trailing_min_profit_pct": 0.05,
        "trailing_floor_min_profit_pct": 0.50,
        "trailing_floor_dynamic_enabled": True,
        "trailing_floor_retreat_pct": 0.20,
        "sports_trailing_floor_retreat_pct": 0.15,
        "trailing_partial_sell_enabled": False,
        "trailing_partial_sell_pct": 0.5,
        "trailing_partial_sell_min_shares": 30,
        "profit_take_pct": 9.99,
        "quick_exit_enabled": False,
        "quick_exit_entry_max": 0.25,
        "quick_exit_profit_pct": 0.05,
        "hold_to_settle_threshold": 0.99,
        "hold_to_settle_hours": 99999,
        "trailing_tiers": [],
        "weather_trailing_min_drop": 0.45,
        "weather_quick_exit_price": 0,
        "weather_quick_entry_max": 0.23,
        "min_hold_minutes": 0,
        "crypto": {
            "override_enabled": True,
            "certainty_sell_threshold": 0.99,
            "near_ceiling_threshold": 0.99,
            "profit_take_pct": 9.99,
            "trailing_drop_pct": 0.15,
            "trailing_min_profit_pct": 0.10,
            "hold_to_settle_threshold": 0.99,
            "hold_to_settle_hours": 99999,
            "max_hold_hours": 99999,
            "trailing_floor_retreat_pct": 0.12,
        },
    }
    if overrides:
        _pt.update(overrides)

    def _get(key):
        if key == "profit_taking":
            return _pt
        if key == "stop_loss":
            return {"weather_rotation_exit": {"enabled": False}}
        if key == "sports_live":
            return {
                "game_window_hours": 4.0,
                "post_expiry_window_hours": 2.0,
                "near_ceiling_threshold": 0.93,
                "profit_take_pct": 0.99,
            }
        return None

    return _get


# ══════════════════════════════════════════════════════════════════
# 1. 动态 floor 随 peak 上升
# ══════════════════════════════════════════════════════════════════


class TestDynamicFloorFollowsPeak:

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_floor_set_on_first_peak(self, mock_get, mock_db):
        """Peak 超过 entry+5% → floor 首次设置。低 peak 时 min_floor 主导。"""
        mock_get.side_effect = _dynamic_pt_config(
            {"trailing_floor_min_profit_pct": 0.02}
        )
        bet = _make_bet(entry_price=0.44, market_id="mkt-dyn")
        # Peak at 0.60 (return 36% > 5%)
        tracker = _make_tracker({"mkt-dyn": 0.60})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []  # current 0.60 > floor

        peak_key = bet["bet_id"]
        # dynamic floor = 0.60 × 0.80 = 0.48
        # min floor = 0.44 × 1.02 = 0.4488
        # max(0.48, 0.4488) = 0.48 (dynamic wins)
        assert peak_key in tracker._floor_prices
        assert abs(tracker._floor_prices[peak_key] - 0.48) < 0.001

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_floor_upgrades_when_peak_rises(self, mock_get, mock_db):
        """Peak 升高后 dynamic floor 也跟着升高。"""
        mock_get.side_effect = _dynamic_pt_config()
        bet = _make_bet(entry_price=0.44, market_id="mkt-up")
        peak_key = bet["bet_id"]

        # Round 1: peak=0.60 → floor = max(0.48, 0.66) = 0.66
        tracker = _make_tracker({"mkt-up": 0.60})
        tracker.check_profit_take_exits([bet])
        assert abs(tracker._floor_prices[peak_key] - 0.66) < 0.001

        # Round 2: peak rises to 0.86 → floor = max(0.86×0.80, 0.66) = max(0.688, 0.66) = 0.688
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-up": 0.86}
        )._fetch_market_statuses
        tracker.check_profit_take_exits([bet])
        assert abs(tracker._floor_prices[peak_key] - 0.688) < 0.001

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_floor_upgrades_further_at_higher_peak(self, mock_get, mock_db):
        """Peak 继续上升 → floor 继续上调。"""
        mock_get.side_effect = _dynamic_pt_config(
            {"trailing_floor_min_profit_pct": 0.02}
        )
        bet = _make_bet(entry_price=0.44, market_id="mkt-hi")
        peak_key = bet["bet_id"]

        # Round 1: peak=0.86 → floor = max(0.86×0.80, 0.4488) = 0.688
        tracker = _make_tracker({"mkt-hi": 0.86})
        tracker.check_profit_take_exits([bet])
        assert abs(tracker._floor_prices[peak_key] - 0.688) < 0.001

        # Round 2: peak=0.94 → floor = max(0.94×0.80, 0.688) = max(0.752, 0.688) = 0.752
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-hi": 0.94}
        )._fetch_market_statuses
        tracker.check_profit_take_exits([bet])
        assert abs(tracker._floor_prices[peak_key] - 0.752) < 0.001


# ══════════════════════════════════════════════════════════════════
# 2. 棘轮：floor 只升不降
# ══════════════════════════════════════════════════════════════════


class TestFloorRatchet:

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_floor_never_decreases(self, mock_get, mock_db):
        """Price 下跌后 floor 不应降低。"""
        mock_get.side_effect = _dynamic_pt_config(
            {"trailing_floor_min_profit_pct": 0.02}
        )
        bet = _make_bet(entry_price=0.44, market_id="mkt-rat")
        peak_key = bet["bet_id"]

        # Round 1: peak=0.90 → floor = max(0.72, 0.4488) = 0.72
        tracker = _make_tracker({"mkt-rat": 0.90})
        tracker.check_profit_take_exits([bet])
        floor_after_peak = tracker._floor_prices[peak_key]
        assert abs(floor_after_peak - 0.72) < 0.001

        # Round 2: price drops to 0.75 (above floor, no exit)
        # peak stays at 0.90 (in _peak_prices)
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-rat": 0.75}
        )._fetch_market_statuses
        tracker.check_profit_take_exits([bet])
        # Floor must remain 0.72, not drop
        assert abs(tracker._floor_prices[peak_key] - 0.72) < 0.001


# ══════════════════════════════════════════════════════════════════
# 3. 赛道特定 retreat_pct
# ══════════════════════════════════════════════════════════════════


class TestCategorySpecificRetreat:

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_sports_uses_tighter_retreat(self, mock_get, mock_db):
        """体育赛道 retreat=0.15 → floor = peak × 0.85。"""
        mock_get.side_effect = _dynamic_pt_config(
            {"trailing_floor_min_profit_pct": 0.02}
        )
        bet = _make_bet(entry_price=0.44, market_id="mkt-sp", category="sports")
        peak_key = bet["bet_id"]

        # peak=0.90 → sports floor = max(0.90×0.85, 0.4488) = 0.765
        tracker = _make_tracker({"mkt-sp": 0.90})
        tracker.check_profit_take_exits([bet])
        assert abs(tracker._floor_prices[peak_key] - 0.765) < 0.001

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_crypto_uses_tightest_retreat(self, mock_get, mock_db):
        """Crypto 赛道 retreat=0.12 → floor = peak × 0.88。"""
        mock_get.side_effect = _dynamic_pt_config(
            {"trailing_floor_min_profit_pct": 0.02}
        )
        bet = _make_bet(entry_price=0.44, market_id="mkt-cr", category="crypto")
        peak_key = bet["bet_id"]

        # peak=0.90 → crypto floor = max(0.90×0.88, 0.4488) = 0.792
        tracker = _make_tracker({"mkt-cr": 0.90})
        tracker.check_profit_take_exits([bet])
        assert abs(tracker._floor_prices[peak_key] - 0.792) < 0.001

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_general_category_uses_default_retreat(self, mock_get, mock_db):
        """非体育非Crypto → 通用 retreat=0.20 → floor = peak × 0.80。"""
        mock_get.side_effect = _dynamic_pt_config(
            {"trailing_floor_min_profit_pct": 0.02}
        )
        bet = _make_bet(entry_price=0.44, market_id="mkt-gen", category="weather")
        peak_key = bet["bet_id"]

        # peak=0.90 → floor = max(0.90×0.80, 0.4488) = 0.72
        tracker = _make_tracker({"mkt-gen": 0.90})
        tracker.check_profit_take_exits([bet])
        assert abs(tracker._floor_prices[peak_key] - 0.72) < 0.001


# ══════════════════════════════════════════════════════════════════
# 4. 关闭动态 → 回退旧静态逻辑
# ══════════════════════════════════════════════════════════════════


class TestDynamicDisabled:

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_disabled_uses_static_floor(self, mock_get, mock_db):
        """dynamic=false → floor = entry × (1 + floor_min_profit_pct)，一次性锁定。"""
        mock_get.side_effect = _dynamic_pt_config(
            {"trailing_floor_dynamic_enabled": False}
        )
        bet = _make_bet(entry_price=0.44, market_id="mkt-stat")
        peak_key = bet["bet_id"]

        # Round 1: peak=0.60 → static floor = 0.44 × 1.50 = 0.66
        tracker = _make_tracker({"mkt-stat": 0.60})
        tracker.check_profit_take_exits([bet])
        assert abs(tracker._floor_prices[peak_key] - 0.66) < 0.001

        # Round 2: peak=0.90 → static floor still 0.66 (棘轮不会降，但static不会升)
        # Actually, new_floor = min_floor = 0.66, old_floor = 0.66 → no update (new <= old)
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-stat": 0.90}
        )._fetch_market_statuses
        tracker.check_profit_take_exits([bet])
        assert abs(tracker._floor_prices[peak_key] - 0.66) < 0.001


# ══════════════════════════════════════════════════════════════════
# 5. Case 90127dfe 模拟
# ══════════════════════════════════════════════════════════════════


class TestCase90127dfe:

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_case_90127dfe_dynamic_floor_improvement(self, mock_get, mock_db):
        """Case 90127dfe: entry=0.44, peak→0.86
        旧: floor 锁定在 0.66 (entry×1.5)
        新: floor 动态上调至 0.688 (0.86×0.80)
        """
        mock_get.side_effect = _dynamic_pt_config()
        bet = _make_bet(entry_price=0.44, market_id="mkt-case")
        peak_key = bet["bet_id"]

        # Simulate progressive peak rise
        prices = [0.50, 0.60, 0.70, 0.80, 0.86]
        tracker = _make_tracker({"mkt-case": prices[0]})

        for p in prices:
            tracker._fetch_market_statuses = _make_tracker(
                {"mkt-case": p}
            )._fetch_market_statuses
            tracker.check_profit_take_exits([bet])

        # Final floor should be peak(0.86) × 0.80 = 0.688
        final_floor = tracker._floor_prices[peak_key]
        assert abs(final_floor - 0.688) < 0.001
        # Verify it's higher than the old static floor (0.66)
        old_static_floor = 0.44 * 1.5
        assert final_floor > old_static_floor

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_case_90127dfe_sports_even_tighter(self, mock_get, mock_db):
        """同一用例，体育赛道 retreat=0.15 → floor=0.731 比通用0.688更高。"""
        mock_get.side_effect = _dynamic_pt_config()
        bet = _make_bet(entry_price=0.44, market_id="mkt-sp-case", category="sports")
        peak_key = bet["bet_id"]

        tracker = _make_tracker({"mkt-sp-case": 0.86})
        tracker.check_profit_take_exits([bet])

        # sports floor = 0.86 × 0.85 = 0.731
        assert abs(tracker._floor_prices[peak_key] - 0.731) < 0.001


# ══════════════════════════════════════════════════════════════════
# 6. Floor 保护触发（与动态 floor 配合）
# ══════════════════════════════════════════════════════════════════


class TestFloorProtectionWithDynamic:

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_floor_protection_triggers_at_dynamic_level(self, mock_get, mock_db):
        """价格跌破动态 floor → floor_protection 触发。"""
        mock_get.side_effect = _dynamic_pt_config(
            {"trailing_floor_min_profit_pct": 0.02}
        )
        bet = _make_bet(entry_price=0.44, market_id="mkt-fpd")
        peak_key = bet["bet_id"]

        # Round 1: peak=0.90 → floor = max(0.72, 0.4488) = 0.72
        tracker = _make_tracker({"mkt-fpd": 0.90})
        tracker.check_profit_take_exits([bet])
        assert abs(tracker._floor_prices[peak_key] - 0.72) < 0.001

        # Round 2: price drops to 0.70 (< floor 0.72, return = (0.70-0.44)/0.44 = +59% > 0)
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-fpd": 0.70}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "floor_protection"

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_floor_protection_skips_when_losing(self, mock_get, mock_db):
        """价格跌破 floor 但 return < 0 → 不触发 floor_protection（给止损处理）。"""
        mock_get.side_effect = _dynamic_pt_config(
            {"trailing_floor_min_profit_pct": 0.02}
        )
        bet = _make_bet(entry_price=0.50, market_id="mkt-loss")
        peak_key = bet["bet_id"]

        # Round 1: peak=0.60 → floor = max(0.60×0.80, 0.51) = max(0.48, 0.51) = 0.51
        tracker = _make_tracker({"mkt-loss": 0.60})
        tracker.check_profit_take_exits([bet])

        # Round 2: price crashes to 0.40 (< floor 0.51, but return = (0.40-0.50)/0.50 = -20%)
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-loss": 0.40}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []  # floor_protection skipped due to negative return


# ══════════════════════════════════════════════════════════════════
# 7. DB 持久化验证
# ══════════════════════════════════════════════════════════════════


class TestFloorDBPersistence:

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_floor_persisted_on_initial_set(self, mock_get, mock_db):
        """Floor 首次设置时调用 db.update_trailing_state。"""
        mock_get.side_effect = _dynamic_pt_config()
        bet = _make_bet(entry_price=0.44, market_id="mkt-dbp")

        tracker = _make_tracker({"mkt-dbp": 0.60})
        tracker.check_profit_take_exits([bet])

        mock_db.update_trailing_state.assert_called()
        call_args = mock_db.update_trailing_state.call_args
        assert call_args[1].get("floor_price") is not None or len(call_args[0]) >= 2

    @patch("src.simulation.outcome_tracker.db")
    @patch("src.simulation.outcome_tracker.get")
    def test_floor_persisted_on_upgrade(self, mock_get, mock_db):
        """Floor 动态上调时再次调用 db.update_trailing_state。"""
        mock_get.side_effect = _dynamic_pt_config()
        bet = _make_bet(entry_price=0.44, market_id="mkt-dbu")

        tracker = _make_tracker({"mkt-dbu": 0.60})
        tracker.check_profit_take_exits([bet])
        first_call_count = mock_db.update_trailing_state.call_count

        # Peak rises → floor should upgrade → another DB call
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-dbu": 0.90}
        )._fetch_market_statuses
        tracker.check_profit_take_exits([bet])
        assert mock_db.update_trailing_state.call_count > first_call_count
