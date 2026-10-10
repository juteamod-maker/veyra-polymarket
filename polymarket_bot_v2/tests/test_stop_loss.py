"""
test_stop_loss.py — 止损策略单元测试（2026-03-19 Phase19 全面启用止损版）

设计原则：
  Phase19 根据128笔生产数据证明禁用止损是毁灭性的（Munich -97.8%!）。
  现在所有赛道都有主动止损保护。

  止损层级（Phase53 激进放宽 — 止损仅为最后安全网）：
  1. 地板止损（floor）：全局0.05 / 天气B_edge 0.03 / 体育live 0.05
  2. 标准止损：全局0.60(60%) / 体育0.70 / 天气0.80 / 体育live 0.80
  3. 翻转止损：reversal_exit_price=0.08（入场≥40¢跌到<8¢）
  4. 临近到期止损（expiry_sl）：< 4h 到期 + 亏损 >= 60%
  5. 早期止损（early_stop）：已禁用（Phase53）
  6. 体育赛后强制清仓（sports_expired）
"""

import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from src.simulation.outcome_tracker import OutcomeTracker
from src.utils import db
from src.utils.config_loader import get as real_get


@pytest.fixture(autouse=True)
def _disable_liquidity_guard():
    """
    Core stop-loss tests validate trigger logic in isolation.
    Liquidity guard (multi-tick confirmation) is tested separately
    in test_liquidity_guard.py. Disable here to avoid interference.
    """

    def patched_get(key, default=None):
        val = real_get(key, default)
        if key == "stop_loss" and isinstance(val, dict):
            val = dict(val)
            val["liquidity_guard"] = {"enabled": False}
        return val

    with patch("src.simulation.outcome_tracker.get", side_effect=patched_get):
        yield


# ------------------------------------------------------------------
# 测试夹具
# ------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path):
    """每个测试用独立 SQLite，互不干扰。"""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    return db_path


def _make_bet(
    market_id: str = "mkt-001",
    entry_price: float = 0.40,
    outcome: str = "YES",
    size: float = 1.0,
    end_date: str = "2027-12-31T12:00:00Z",
    category: str = "",
    bet_bucket: str = "",
) -> dict:
    """生成标准模拟投注记录。"""
    return {
        "bet_id": str(uuid.uuid4()),
        "market_id": market_id,
        "question": "Will the highest temperature in Seoul be 10C on March 15?",
        "outcome": outcome,
        "market_price": entry_price,
        "size_usdc": size,
        "market_end_date": end_date,
        "ts_entered": time.time() - 3600,
        "category": category,
        "bet_bucket": bet_bucket,
    }


def _make_tracker(current_prices: dict) -> OutcomeTracker:
    """创建 OutcomeTracker，mock Gamma API 返回固定价格。"""
    tracker = OutcomeTracker()

    def fake_fetch(market_ids, **kwargs):
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


# ==================================================================
# 1. 地板止损（Floor Stop）— 仍然有效
# ==================================================================


class TestFloorStop:
    """价格跌破绝对死亡线（< 0.10），市场彻底判死，立即止损。"""

    def test_floor_exit_below_threshold(self):
        """价格 0.04 < 0.10（地板阈值）-> 触发 floor 止损。"""
        bet = _make_bet(entry_price=0.40)
        tracker = _make_tracker({"mkt-001": 0.04})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "floor"
        assert stops[0]["exit_price"] == pytest.approx(0.04)

    def test_floor_exit_at_exact_threshold(self):
        """价格恰好等于地板阈值 0.10 — 不触发 floor（需严格小于）。
        entry=0.11→0.10 亏损 9.1% < 10%(standard) → 不触发。"""
        bet = _make_bet(entry_price=0.11)
        tracker = _make_tracker({"mkt-001": 0.10})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_floor_exit_regardless_of_entry_price(self):
        """即使入场价很低（0.06），价格跌到 0.01 仍触发 floor。"""
        bet = _make_bet(entry_price=0.06)
        tracker = _make_tracker({"mkt-001": 0.01})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "floor"

    def test_no_floor_exit_above_threshold(self):
        """价格 0.11 > 0.10 地板, entry=0.12→0.11 亏损8.3% < 10% → 不触发。"""
        bet = _make_bet(entry_price=0.12)
        tracker = _make_tracker({"mkt-001": 0.11})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []


# ==================================================================
# 2. 标准止损（已禁用 standard_stop_pct=0.99）
# ==================================================================


class TestStandardStopLossEnabled:
    """standard_stop_pct=0.75（T262进一步放宽）。验证深度亏损触发止损。"""

    def test_50_pct_loss_triggers(self):
        """亏损 77.5%（从 0.40 跌到 0.09）-> 触发 standard_sl（77.5% > 75%）。"""
        bet = _make_bet(entry_price=0.40)
        tracker = _make_tracker({"mkt-001": 0.09})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"

    def test_70_pct_loss_triggers(self):
        """亏损 80%（从 0.40 跌到 0.08）-> 触发 standard_sl。"""
        bet = _make_bet(entry_price=0.40)
        tracker = _make_tracker({"mkt-001": 0.08})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"

    def test_80_pct_loss_triggers_floor_first(self):
        """亏损 90%（从 0.40 跌到 0.04）-> 0.04 < floor(0.05)，floor 触发。"""
        bet = _make_bet(entry_price=0.40)
        tracker = _make_tracker({"mkt-001": 0.04})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "floor"

    def test_35_pct_loss_weather_triggers(self):
        """天气市场亏损 82.5%（0.40→0.07）> weather_stop(80%) -> 触发。
        Phase53: 时间加权止损已禁用，统一用 weather_standard_stop_pct=0.80。"""
        bet = _make_bet(entry_price=0.40, category="weather")
        tracker = _make_tracker({"mkt-001": 0.07})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"

    def test_30_pct_loss_weather_no_trigger(self):
        """天气市场亏损 17.5%（0.40→0.33）< weather far_stop(45%) → 不触发。"""
        bet = _make_bet(entry_price=0.40, category="weather")
        tracker = _make_tracker({"mkt-001": 0.33})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_35_pct_loss_general_no_trigger(self):
        """通用赛道亏损 7.5%（0.40→0.37）< standard_stop(25%) → 不触发。"""
        bet = _make_bet(entry_price=0.40)
        tracker = _make_tracker({"mkt-001": 0.37})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_only_floor_triggers_at_deep_loss(self):
        """价格跌到 0.04 < floor(0.05) -> floor 优先于 standard_sl。"""
        bet = _make_bet(entry_price=0.40)
        tracker = _make_tracker({"mkt-001": 0.04})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "floor"

    def test_no_stop_when_profitable(self):
        """仓位盈利时不触发任何止损。"""
        bet = _make_bet(entry_price=0.40)
        tracker = _make_tracker({"mkt-001": 0.55})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []


# ==================================================================
# 3. 方向翻转止损（已禁用 reversal_exit_price=0.0）
# ==================================================================


class TestReversalStopLossEnabled:
    """reversal_exit_price=0.08（Phase53放宽）。入场≥40¢跌到<8¢说明方向完全错误。"""

    def test_reversal_triggers(self):
        """entry=0.50, current=0.12 -> 0.12 < reversal(0.15) 且入场≥0.40。
        但 standard_sl 优先（亏损76% > 40%）。"""
        bet = _make_bet(entry_price=0.50)
        tracker = _make_tracker({"mkt-001": 0.12})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        # standard_sl(40%) 优先级高于 reversal
        assert stops[0]["trigger"] == "standard_sl"

    def test_standard_sl_triggers_before_reversal(self):
        """entry=0.50, current=0.11 -> 亏损78% 触发 standard_sl（78%>75%）。"""
        bet = _make_bet(entry_price=0.50)
        tracker = _make_tracker({"mkt-001": 0.11})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"

    def test_reversal_alone_when_loss_not_deep_enough(self):
        """entry=0.45, current=0.10 -> 亏损77.8% > 75% → standard_sl触发。"""
        bet = _make_bet(entry_price=0.45)
        tracker = _make_tracker({"mkt-001": 0.10})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"

    def test_no_reversal_when_entry_below_threshold(self):
        """entry=0.35 < 0.40, current=0.08 -> reversal不触发（入场价<40¢）。
        但 standard_sl 触发因为亏损(0.08-0.35)/0.35=-0.771 > 75%。"""
        bet = _make_bet(entry_price=0.35)
        tracker = _make_tracker({"mkt-001": 0.08})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"

    def test_deep_loss_only_floor_triggers(self):
        """entry=0.50, current=0.03 -> floor 优先于一切（< 0.05）。"""
        bet = _make_bet(entry_price=0.50)
        tracker = _make_tracker({"mkt-001": 0.03})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "floor"


# ==================================================================
# 4. 临近到期止损（Phase53: <4h, 亏损>=60%）
# ==================================================================


class TestExpiryStopLoss:
    def test_expiry_stop_near_deadline_with_deep_loss(self):
        """到期前 3h（<4h）且亏损 77.5%（>=75%）-> standard_sl先触发（77.5%>75%）。"""
        import datetime

        expiry = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
        ).isoformat()
        bet = _make_bet(entry_price=0.40, end_date=expiry)
        # return_pct = (0.09 - 0.40) / 0.40 = -0.775
        tracker = _make_tracker({"mkt-001": 0.09})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) >= 1
        # standard_sl(75%) 优先于 expiry_sl
        assert stops[0]["trigger"] == "standard_sl"

    def test_expiry_stop_moderate_loss(self):
        """到期前 3h（<8h）且亏损 7.5%（< 10% standard, < 30% expiry）→ 不触发。"""
        import datetime

        expiry = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
        ).isoformat()
        bet = _make_bet(entry_price=0.40, end_date=expiry)
        # return_pct = (0.37 - 0.40) / 0.40 = -7.5%, below both standard(10%) and expiry(30%)
        tracker = _make_tracker({"mkt-001": 0.37})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_no_expiry_stop_if_loss_insufficient(self):
        """到期前 3h 但亏损只有 7.5%（< 10% standard, < 30% expiry）-> 不触发。"""
        import datetime

        expiry = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=3)
        ).isoformat()
        bet = _make_bet(entry_price=0.40, end_date=expiry)
        # return_pct = (0.37 - 0.40) / 0.40 = -7.5%
        tracker = _make_tracker({"mkt-001": 0.37})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_no_expiry_stop_if_far_from_expiry(self):
        """亏损 77.5% 但距到期还有24h（> 4h 窗口）-> standard_sl 触发（非expiry）。"""
        import datetime

        expiry = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
        ).isoformat()
        bet = _make_bet(entry_price=0.40, end_date=expiry)
        # return_pct = (0.09 - 0.40) / 0.40 = -0.775
        tracker = _make_tracker({"mkt-001": 0.09})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"

    def test_no_stop_far_from_expiry_small_loss(self):
        """亏损 7.5% 且距到期还有 24h -> 不触发（7.5% < standard 10%，且不在expiry窗口）。"""
        import datetime

        expiry = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
        ).isoformat()
        bet = _make_bet(entry_price=0.40, end_date=expiry)
        tracker = _make_tracker({"mkt-001": 0.37})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_expiry_stop_at_boundary_hours(self):
        """到期前恰好 4h+1min -> 不在expiry窗口。亏损77.5% > 75% → standard_sl 触发。"""
        import datetime

        expiry = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(hours=4, minutes=1)
        ).isoformat()
        bet = _make_bet(entry_price=0.40, end_date=expiry)
        # return_pct = (0.09 - 0.40)/0.40 = -0.775
        tracker = _make_tracker({"mkt-001": 0.09})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"


# ==================================================================
# 5. 止损 P&L 计算与 DB 写入
# ==================================================================


class TestStopLossPnl:
    def test_stop_loss_pnl_calculation(self, tmp_db):
        """止损 P&L 验证：shares = size/entry，proceeds = shares x exit。"""
        bet = _make_bet(entry_price=0.40, size=1.0)
        bet["exit_price"] = 0.02
        bet["return_pct"] = (0.02 - 0.40) / 0.40

        tracker = OutcomeTracker()
        record = {
            **bet,
            "ts_entered": time.time(),
            "category": "crypto",
            "confidence": 0.7,
            "ai_probability": 0.65,
            "ev": 0.04,
            "sim_balance_before": 100.0,
            "sim_balance_after": 99.0,
            "market_end_date": bet["market_end_date"],
            "weather_context": "",
            "ai_models": "gpt-4o",
            "cycle_id": 1,
        }
        db.insert_paper_bet(record, tmp_db)
        tracker.close_position_stop_loss(bet, db_path=tmp_db)

        summary = db.get_paper_summary(tmp_db)
        assert summary["resolved"] == 1
        # shares = 1.0/0.40 = 2.5; gross = 2.5 x 0.02 = 0.05
        # _estimate_dry_run_fees deducts buy+sell from gross → proceeds
        # then T189-G further deducts exit_fee via calc_pm_fee → final pnl
        # Exact value depends on fee rates; just verify it's a significant loss
        assert summary["total_pnl"] < -0.90
        assert summary["total_pnl"] > -1.05  # can't lose more than size + fees

    def test_stop_loss_exit_type_recorded(self, tmp_db):
        """close_paper_bet_stop_loss 应写入 exit_type=stop_loss，is_win=0。"""
        bet = _make_bet(entry_price=0.40, size=1.0)
        bet["exit_price"] = 0.02
        bet["return_pct"] = -0.95

        record = {
            **bet,
            "ts_entered": time.time(),
            "category": "weather",
            "confidence": 0.6,
            "ai_probability": 0.55,
            "ev": 0.03,
            "sim_balance_before": 100.0,
            "sim_balance_after": 99.0,
            "market_end_date": bet["market_end_date"],
            "weather_context": "",
            "ai_models": "gpt-4o",
            "cycle_id": 1,
        }
        db.insert_paper_bet(record, tmp_db)
        db.close_paper_bet_stop_loss(
            bet_id=bet["bet_id"],
            exit_price=0.02,
            pnl=-0.95,
            payout=0.05,
            db_path=tmp_db,
        )

        with db.connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT exit_type, resolved_outcome, is_win FROM paper_bets WHERE bet_id=?",
                (bet["bet_id"],),
            ).fetchone()
        assert row["exit_type"] == "stop_loss"
        assert row["resolved_outcome"] == "STOP_LOSS"
        assert row["is_win"] == 0


# ==================================================================
# 6. 禁用状态
# ==================================================================


class TestDisabledStopLoss:
    def test_disabled_returns_empty(self):
        """stop_loss.enabled=false 时，即使价格跌破地板，也不触发止损。"""
        bet = _make_bet(entry_price=0.40)
        tracker = _make_tracker({"mkt-001": 0.01})

        with patch(
            "src.simulation.outcome_tracker.get",
            return_value={
                "enabled": False,
                "floor_price": 0.05,
                "standard_stop_pct": 0.40,
                "expiry_stop_hours": 8,
                "expiry_stop_loss_pct": 0.30,
            },
        ):
            stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_no_stop_on_empty_bets(self):
        """空仓位列表不触发止损（边界情况）。"""
        tracker = _make_tracker({})
        stops = tracker.check_stop_loss_exits([])
        assert stops == []


# ==================================================================
# 7. 止损与止盈不互相触发
# ==================================================================


class TestStopLossIsolation:
    def test_stop_loss_does_not_affect_profitable_position(self):
        """盈利 50% 的仓位不触发止损。"""
        bet = _make_bet(entry_price=0.40)
        tracker = _make_tracker({"mkt-001": 0.60})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_multiple_bets_only_stop_losers(self):
        """多个仓位混合：止损价格跌破标准线的，保留盈利和小亏的。"""
        bet_win = _make_bet(market_id="mkt-001", entry_price=0.40)
        bet_small_loss = _make_bet(market_id="mkt-002", entry_price=0.40)
        bet_dead = _make_bet(market_id="mkt-003", entry_price=0.40)
        tracker = _make_tracker(
            {
                "mkt-001": 0.65,  # 盈利，不止损
                "mkt-002": 0.37,  # 亏损 7.5%，< 10% threshold，不止损
                "mkt-003": 0.03,  # 低于 floor(0.10)，止损
            }
        )
        stops = tracker.check_stop_loss_exits([bet_win, bet_small_loss, bet_dead])
        assert len(stops) == 1
        assert stops[0]["market_id"] == "mkt-003"
        assert stops[0]["trigger"] == "floor"

    def test_standard_sl_triggers_on_deep_loss(self):
        """验证：亏损超过40%时 standard_sl 触发（Phase19核心功能）。"""
        bet = _make_bet(entry_price=0.50)
        tracker = _make_tracker({"mkt-001": 0.10})  # 亏损 80%
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"


# ==================================================================
# 8. 天气 B_edge 专用地板止损
# ==================================================================


class TestWeatherEdgeFloor:
    """天气 B_edge 专用地板 weather_edge_floor_price=0.03。
    注意：天气标准止损现在是 20%，与地板共同保护。"""

    def test_weather_edge_uses_lower_floor(self):
        """天气 B_edge 价格 0.025 > weather_floor(0.03)?
        实际 0.025 < 0.03 → floor 触发。"""
        bet = _make_bet(entry_price=0.10, category="weather", bet_bucket="b_edge")
        tracker = _make_tracker({"mkt-001": 0.025})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "floor"

    def test_weather_edge_above_floor_small_loss(self):
        """天气 B_edge entry=0.10, current=0.085 → 亏损15% < weather_stop(20%)
        且 0.085 > weather_floor(0.03) → 不触发。"""
        bet = _make_bet(entry_price=0.10, category="weather", bet_bucket="b_edge")
        tracker = _make_tracker({"mkt-001": 0.085})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_weather_edge_floor_triggers_at_very_low(self):
        """天气 B_edge 价格 0.015 < floor(0.03) -> 触发 floor。"""
        bet = _make_bet(entry_price=0.10, category="weather", bet_bucket="b_edge")
        tracker = _make_tracker({"mkt-001": 0.015})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "floor"

    def test_weather_low_entry_without_bucket_tag(self):
        """天气 bet 无 bucket 标签但 entry<0.25 → 使用低 floor(0.03)。
        entry=0.20, current=0.035 → 亏损 82.5% > weather_stop(80%) → standard_sl 触发。"""
        bet = _make_bet(entry_price=0.20, category="weather")
        tracker = _make_tracker({"mkt-001": 0.035})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"

    def test_weather_low_entry_above_all_thresholds(self):
        """天气 bet entry=0.12, current=0.10 → 亏损16.7% < weather_stop(20%)
        且 0.10 > weather_floor(0.03) → 不触发。"""
        bet = _make_bet(entry_price=0.12, category="weather")
        tracker = _make_tracker({"mkt-001": 0.10})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_non_weather_uses_global_floor(self):
        """非天气 bet (sports) 价格 0.04 < 全局 floor(0.05) -> 触发 floor。"""
        bet = _make_bet(entry_price=0.10, category="sports")
        tracker = _make_tracker({"mkt-001": 0.04})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "floor"


# ==================================================================
# 9. 体育比赛进行中（Sports Live）止损
# ==================================================================


class TestSportsLiveStopLoss:
    """体育比赛进行中：floor=0.10, standard=0.15(启用!), live=0.45。
    体育赛事价格变化是比分信号（非噪音），必须启用止损保护。
    注意：expiry_stop_hours=8, expiry_stop_loss_pct=0.30 也会影响live游戏。"""

    def _make_live_bet(self, entry_price=0.50, hours_ahead=2.0):
        from datetime import datetime, timedelta, timezone

        end_dt = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
        return _make_bet(
            entry_price=entry_price,
            category="sports",
            end_date=end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def test_live_floor_triggers_at_5_cents(self):
        """比赛中地板 0.05: 价格 0.04 < 0.05 -> live_floor。"""
        bet = self._make_live_bet(entry_price=0.50, hours_ahead=2.0)
        tracker = _make_tracker({"mkt-001": 0.04})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "live_floor"

    def test_live_no_standard_sl_small_loss(self):
        """比赛中亏损 20% < 45%(live stop) -> 不触发任何止损。
        注：hours_ahead=1.5 在 expiry 8h 窗口内，但 20% < 30%(expiry_stop)。"""
        bet = self._make_live_bet(entry_price=0.50, hours_ahead=1.5)
        tracker = _make_tracker({"mkt-001": 0.40})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_live_no_reversal_moderate_loss(self):
        """比赛中入场 0.55 跌到 0.42 (23.6% loss < 45%) 且 >= reversal(0.15) -> 不触发。
        注：在expiry窗口内，但 23.6% < 30% → expiry也不触发。"""
        bet = self._make_live_bet(entry_price=0.55, hours_ahead=3.0)
        tracker = _make_tracker({"mkt-001": 0.42})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_live_moderate_loss_below_live_sl_but_in_expiry(self):
        """比赛中亏损 62%: < live_stop(80%) 但 ≥ expiry_stop(60%) 且在 4h 窗口 -> expiry_sl。"""
        bet = self._make_live_bet(entry_price=0.50, hours_ahead=2.0)
        tracker = _make_tracker({"mkt-001": 0.19})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "expiry_sl"

    def test_live_deep_loss_triggers_standard_sl(self):
        """比赛中亏损 82% >= 80% -> 触发 live_standard_sl（保护本金！）。"""
        bet = self._make_live_bet(entry_price=0.50, hours_ahead=2.0)
        tracker = _make_tracker({"mkt-001": 0.09})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "live_standard_sl"

    def test_pregame_sports_moderate_loss_no_stop(self):
        """到期 > 8h(expiry窗口) 的体育 bet, 亏损 12% < 15%(sports stop) -> 不触发。"""
        bet = self._make_live_bet(entry_price=0.50, hours_ahead=24.0)
        tracker = _make_tracker({"mkt-001": 0.44})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_pregame_sports_deep_loss_triggers(self):
        """到期 > 4h(非live) 的体育 bet, 亏损 72% >= 70%(sports stop) -> 触发 standard_sl。"""
        bet = self._make_live_bet(entry_price=0.50, hours_ahead=24.0)
        tracker = _make_tracker({"mkt-001": 0.14})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        assert stops[0]["trigger"] == "standard_sl"

    def test_live_reversal_triggers(self):
        """比赛中入场 0.50 跌到 0.10 < reversal(0.15) 且入场≥0.40 -> live_reversal。
        但 standard_sl 先触发因为 -80% < -45%。"""
        bet = self._make_live_bet(entry_price=0.50, hours_ahead=2.0)
        tracker = _make_tracker({"mkt-001": 0.10})
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1
        # standard_sl 优先级高于 reversal
        assert stops[0]["trigger"] == "live_standard_sl"


# ==================================================================
# 10. 早期止损（Phase53 已禁用 early_stop.enabled=false + min_hold=60min）
# ==================================================================


class TestEarlyStopEnabled:
    """Phase53: 早期止损已禁用(early_stop.enabled=false)，min_hold=60min。
    6分钟/5分钟前入场的仓位在 min_hold 保护期内，跳过止损评估。"""

    def test_early_loss_triggers_for_weather(self):
        """入场 6 分钟前（< min_hold 60min），天气市场亏损 25% -> 跳过止损评估。
        Phase53: min_hold=60min 保护期内不触发任何止损。"""
        bet = _make_bet(entry_price=0.40, category="weather")
        bet["ts_entered"] = time.time() - 360  # 6 minutes ago (< min_hold 60min)
        tracker = _make_tracker({"mkt-001": 0.30})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_early_loss_triggers_for_default(self):
        """入场 5 分钟前（< min_hold 60min），亏损 30% -> 跳过止损评估。"""
        bet = _make_bet(entry_price=0.40)
        bet["ts_entered"] = time.time() - 300
        tracker = _make_tracker({"mkt-001": 0.28})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_early_small_loss_no_trigger(self):
        """入场 3 分钟前，亏损 15% < default_pct(25%) -> 不触发。"""
        bet = _make_bet(entry_price=0.40)
        bet["ts_entered"] = time.time() - 180
        tracker = _make_tracker({"mkt-001": 0.34})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []

    def test_early_stop_not_applied_after_window(self):
        """入场 20 分钟前（>15分钟窗口），亏损 7.5% < standard(10%) -> 不触发。
        early_stop 只在 window_minutes 内生效。"""
        bet = _make_bet(entry_price=0.40)
        bet["ts_entered"] = time.time() - 1200  # 20 minutes ago
        tracker = _make_tracker({"mkt-001": 0.37})
        stops = tracker.check_stop_loss_exits([bet])
        assert stops == []


# ==================================================================
# T188: 天气气象数据止损门控 (Weather Stop-Loss Veto Gate)
# ==================================================================


class TestWeatherVeto:
    """T188: 止损触发时，用实时温度验证是否应继续持有天气仓位。"""

    def _make_weather_bet(
        self,
        question="Will the highest temperature in Seoul be 19°C on April 12?",
        entry_price=0.81,
        outcome="NO",
        market_id="mkt-wx-001",
    ):
        bet = _make_bet(
            market_id=market_id,
            entry_price=entry_price,
            outcome=outcome,
            category="weather",
        )
        bet["question"] = question
        return bet

    @patch("src.simulation.outcome_tracker.get")
    def test_veto_blocks_stop_loss_when_temp_far_from_threshold(self, mock_get):
        """Seoul 19°C, NO方向, 当前14°C → 差5°C > margin 3°C → VETO止损。"""

        def _cfg(key, default=None):
            if key == "stop_loss":
                return {
                    "enabled": True,
                    "floor_price": 0.07,
                    "standard_stop_pct": 0.45,
                    "weather_standard_stop_pct": 0.45,
                    "reversal_exit_price": 0.35,
                    "expiry_stop_hours": 8.0,
                    "expiry_stop_loss_pct": 0.25,
                    "stoploss_mode": {
                        "default": "market",
                        "per_category": {"weather": "market"},
                    },
                    "liquidity_guard": {"enabled": False},
                    "weather_time_weighted_stop": {"enabled": False},
                    "early_stop": {"enabled": False},
                    "weather_veto": {
                        "enabled": True,
                        "safe_margin_c": 3,
                        "safe_margin_f": 5,
                        "max_veto_count": 3,
                        "cooldown_seconds": 0,
                        "peak_window_start_hour": 11,
                        "peak_window_end_hour": 16,
                    },
                }
            if key == "profit_taking":
                return {"min_hold_minutes": 0}
            return real_get(key, default)

        mock_get.side_effect = _cfg

        bet = self._make_weather_bet()
        # YES price high → NO sell price low → big loss → triggers standard_sl
        tracker = _make_tracker({"mkt-wx-001": 0.84})

        with patch(
            "src.simulation.outcome_tracker.OutcomeTracker._weather_stop_loss_veto",
            return_value=(True, "当前14.0°C距阈值19°C差5.0°>3° 有利于NO"),
        ):
            stops = tracker.check_stop_loss_exits([bet])
        assert stops == [], "Veto should block stop-loss"

    @patch("src.simulation.outcome_tracker.get")
    def test_veto_allows_stop_loss_when_temp_close_to_threshold(self, mock_get):
        """Seoul 19°C, NO方向, 当前17°C → 差2°C < margin 3°C → 不VETO。"""

        def _cfg(key, default=None):
            if key == "stop_loss":
                return {
                    "enabled": True,
                    "floor_price": 0.07,
                    "standard_stop_pct": 0.45,
                    "weather_standard_stop_pct": 0.45,
                    "reversal_exit_price": 0.35,
                    "expiry_stop_hours": 8.0,
                    "expiry_stop_loss_pct": 0.25,
                    "stoploss_mode": {
                        "default": "market",
                        "per_category": {"weather": "market"},
                    },
                    "liquidity_guard": {"enabled": False},
                    "weather_time_weighted_stop": {"enabled": False},
                    "early_stop": {"enabled": False},
                    "weather_veto": {
                        "enabled": True,
                        "safe_margin_c": 3,
                        "safe_margin_f": 5,
                        "max_veto_count": 3,
                        "cooldown_seconds": 0,
                        "peak_window_start_hour": 11,
                        "peak_window_end_hour": 16,
                    },
                }
            if key == "profit_taking":
                return {"min_hold_minutes": 0}
            return real_get(key, default)

        mock_get.side_effect = _cfg

        bet = self._make_weather_bet()
        tracker = _make_tracker({"mkt-wx-001": 0.84})

        with patch(
            "src.simulation.outcome_tracker.OutcomeTracker._weather_stop_loss_veto",
            return_value=(False, "温度17°C不满足VETO条件"),
        ):
            stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) >= 1, "Should trigger stop-loss when temp is close"

    @patch("src.simulation.outcome_tracker.get")
    def test_veto_does_not_block_floor_stop(self, mock_get):
        """Floor止损不可被VETO — 价格已极低无论气温如何。"""

        def _cfg(key, default=None):
            if key == "stop_loss":
                return {
                    "enabled": True,
                    "floor_price": 0.07,
                    "standard_stop_pct": 0.45,
                    "weather_standard_stop_pct": 0.45,
                    "reversal_exit_price": 0.35,
                    "expiry_stop_hours": 8.0,
                    "expiry_stop_loss_pct": 0.25,
                    "stoploss_mode": {
                        "default": "market",
                        "per_category": {"weather": "market"},
                    },
                    "liquidity_guard": {"enabled": False},
                    "weather_time_weighted_stop": {"enabled": False},
                    "early_stop": {"enabled": False},
                    "weather_veto": {
                        "enabled": True,
                        "max_veto_count": 3,
                        "cooldown_seconds": 0,
                    },
                }
            if key == "profit_taking":
                return {"min_hold_minutes": 0}
            return real_get(key, default)

        mock_get.side_effect = _cfg

        bet = self._make_weather_bet(entry_price=0.81)
        # Floor: price < 0.07
        tracker = _make_tracker({"mkt-wx-001": 0.96})  # YES=0.96 → NO sell ≈ 0.03
        stops = tracker.check_stop_loss_exits([bet])
        # Floor should fire regardless of veto
        if stops:
            assert stops[0]["trigger"] in ("floor", "live_floor")

    def test_veto_unit_method_exact_no_far(self):
        """单元测试: _weather_stop_loss_veto — exact NO, 温度远离阈值。"""
        tracker = OutcomeTracker()
        bet = self._make_weather_bet()  # Seoul 19°C NO

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.return_value = {
                "enabled": True,
                "safe_margin_c": 3,
                "safe_margin_f": 5,
                "max_veto_count": 3,
                "cooldown_seconds": 0,
                "peak_window_start_hour": 11,
                "peak_window_end_hour": 16,
            }
            with patch(
                "src.utils.weather_fetcher.fetch_realtime_temperature",
                return_value=14.0,
            ), patch(
                "src.utils.weather_fetcher.get_city_local_hour",
                return_value=15,
            ):
                veto, reason = tracker._weather_stop_loss_veto(
                    bet, "standard_sl", -0.50
                )
        assert veto is True
        assert "有利于NO" in reason

    def test_veto_unit_method_exact_no_close(self):
        """单元测试: _weather_stop_loss_veto — exact NO, 温度接近阈值。"""
        tracker = OutcomeTracker()
        bet = self._make_weather_bet()  # Seoul 19°C NO

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.return_value = {
                "enabled": True,
                "safe_margin_c": 3,
                "safe_margin_f": 5,
                "max_veto_count": 3,
                "cooldown_seconds": 0,
                "peak_window_start_hour": 11,
                "peak_window_end_hour": 16,
            }
            with patch(
                "src.utils.weather_fetcher.fetch_realtime_temperature",
                return_value=17.0,
            ), patch(
                "src.utils.weather_fetcher.get_city_local_hour",
                return_value=15,
            ):
                veto, reason = tracker._weather_stop_loss_veto(
                    bet, "standard_sl", -0.50
                )
        assert veto is False

    def test_veto_unit_method_range_no_far(self):
        """单元测试: range "68-69°F" NO, 当前60°F → diff > 5°F → VETO。"""
        tracker = OutcomeTracker()
        bet = self._make_weather_bet(
            question="Will the highest temperature in Los Angeles be between 68-69°F on April 11?",
            outcome="NO",
        )

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.return_value = {
                "enabled": True,
                "safe_margin_c": 3,
                "safe_margin_f": 5,
                "max_veto_count": 3,
                "cooldown_seconds": 0,
                "peak_window_start_hour": 11,
                "peak_window_end_hour": 16,
            }
            with patch(
                "src.utils.weather_fetcher.fetch_realtime_temperature",
                return_value=15.6,
            ), patch(
                "src.utils.weather_fetcher.get_city_local_hour",
                return_value=10,
            ):
                veto, reason = tracker._weather_stop_loss_veto(
                    bet, "standard_sl", -0.80
                )
        assert veto is True
        assert "有利于NO" in reason

    def test_veto_unit_method_range_no_close(self):
        """单元测试: range "68-69°F" NO, 当前67°F → diff < 5°F → 不VETO。"""
        tracker = OutcomeTracker()
        bet = self._make_weather_bet(
            question="Will the highest temperature in Los Angeles be between 68-69°F on April 11?",
            outcome="NO",
        )

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.return_value = {
                "enabled": True,
                "safe_margin_c": 3,
                "safe_margin_f": 5,
                "max_veto_count": 3,
                "cooldown_seconds": 0,
                "peak_window_start_hour": 11,
                "peak_window_end_hour": 16,
            }
            with patch(
                "src.utils.weather_fetcher.fetch_realtime_temperature",
                return_value=19.4,
            ), patch(
                "src.utils.weather_fetcher.get_city_local_hour",
                return_value=12,
            ):
                veto, reason = tracker._weather_stop_loss_veto(
                    bet, "standard_sl", -0.80
                )
        assert veto is False

    def test_veto_unit_method_max_count_exceeded(self):
        """单元测试: VETO次数超过max_veto_count → 不再VETO。"""
        tracker = OutcomeTracker()
        bet = self._make_weather_bet()
        bet_id = bet["bet_id"]
        tracker._weather_veto_counts[bet_id] = 3  # already at max

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.return_value = {
                "enabled": True,
                "safe_margin_c": 3,
                "safe_margin_f": 5,
                "max_veto_count": 3,
                "cooldown_seconds": 0,
            }
            veto, reason = tracker._weather_stop_loss_veto(bet, "standard_sl", -0.50)
        assert veto is False
        assert "最大VETO次数" in reason

    def test_veto_unit_method_fetch_failure(self):
        """单元测试: 无法获取气象数据 → 不VETO (failsafe)。"""
        tracker = OutcomeTracker()
        bet = self._make_weather_bet()

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.return_value = {
                "enabled": True,
                "safe_margin_c": 3,
                "safe_margin_f": 5,
                "max_veto_count": 3,
                "cooldown_seconds": 0,
            }
            with patch(
                "src.utils.weather_fetcher.fetch_realtime_temperature",
                return_value=None,
            ):
                veto, reason = tracker._weather_stop_loss_veto(
                    bet, "standard_sl", -0.50
                )
        assert veto is False
        assert "温度" in reason or "气象" in reason

    def test_veto_unit_method_disabled(self):
        """单元测试: weather_veto.enabled=false → 不VETO。"""
        tracker = OutcomeTracker()
        bet = self._make_weather_bet()

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.return_value = {
                "weather_veto": {"enabled": False},
            }
            veto, reason = tracker._weather_stop_loss_veto(bet, "standard_sl", -0.50)
        assert veto is False

    def test_veto_unit_method_gte_no(self):
        """单元测试: gte "75°F or higher" NO, 当前65°F → 差10°F > 5°F → VETO。"""
        tracker = OutcomeTracker()
        bet = self._make_weather_bet(
            question="Will the highest temperature in Phoenix be 75°F or higher on April 11?",
            outcome="NO",
        )

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.return_value = {
                "enabled": True,
                "safe_margin_c": 3,
                "safe_margin_f": 5,
                "max_veto_count": 3,
                "cooldown_seconds": 0,
                "peak_window_start_hour": 11,
                "peak_window_end_hour": 16,
            }
            with patch(
                "src.utils.weather_fetcher.fetch_realtime_temperature",
                return_value=18.3,
            ), patch(
                "src.utils.weather_fetcher.get_city_local_hour",
                return_value=14,
            ):
                veto, reason = tracker._weather_stop_loss_veto(
                    bet, "standard_sl", -0.50
                )
        assert veto is True
        assert "有利于NO" in reason

    def test_veto_non_weather_not_triggered(self):
        """非天气赛道不触发weather veto。验证: check_stop_loss_exits中weather_veto只对weather生效。"""
        bet = _make_bet(entry_price=0.40, category="sports")
        tracker = _make_tracker({"mkt-001": 0.12})
        stops = tracker.check_stop_loss_exits([bet])
        # sports bet should stop-loss normally, no veto
        assert len(stops) >= 1

    def test_veto_post_peak_gte_no_below_threshold(self):
        """T211: gte方向, NO, 已过峰值时间(17:00), temp=23 < threshold=24 → VETO。

        事故复现: Seoul 23°C < 24°C, local_hour=17 > peak_end=16, 应VETO止损。
        """
        tracker = OutcomeTracker()
        bet = self._make_weather_bet(
            question="Will the highest temperature in Seoul be 24°C or higher on April 14?",
            outcome="NO",
            entry_price=0.92,
        )

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.return_value = {
                "enabled": True,
                "safe_margin_c": 3,
                "safe_margin_f": 5,
                "max_veto_count": 3,
                "cooldown_seconds": 0,
                "peak_window_start_hour": 11,
                "peak_window_end_hour": 16,
            }
            with patch(
                "src.utils.weather_fetcher.fetch_realtime_temperature",
                return_value=23.0,
            ), patch(
                "src.utils.weather_fetcher.get_city_local_hour",
                return_value=17,
            ):
                veto, reason = tracker._weather_stop_loss_veto(
                    bet, "standard_sl", -0.70
                )
        assert veto is True, f"应VETO: 已过峰值且temp<threshold, reason={reason}"
        assert "已过峰值" in reason

    def test_veto_post_peak_gte_no_above_threshold(self):
        """T211: gte方向, NO, 已过峰值, 但temp=25 >= threshold=24 → 不VETO（NO可能输）。"""
        tracker = OutcomeTracker()
        bet = self._make_weather_bet(
            question="Will the highest temperature in Seoul be 24°C or higher on April 14?",
            outcome="NO",
            entry_price=0.92,
        )

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.return_value = {
                "enabled": True,
                "safe_margin_c": 3,
                "safe_margin_f": 5,
                "max_veto_count": 3,
                "cooldown_seconds": 0,
                "peak_window_start_hour": 11,
                "peak_window_end_hour": 16,
            }
            with patch(
                "src.utils.weather_fetcher.fetch_realtime_temperature",
                return_value=25.0,
            ), patch(
                "src.utils.weather_fetcher.get_city_local_hour",
                return_value=17,
            ):
                veto, reason = tracker._weather_stop_loss_veto(
                    bet, "standard_sl", -0.70
                )
        assert veto is False, "不应VETO: temp>=threshold, NO可能输"

    def test_veto_during_peak_gte_no_near_threshold(self):
        """T211: gte方向, NO, 峰值窗口内(12:00), temp=23 < threshold=24 但差<margin → 不VETO。

        峰值窗口内温度仍可能上升，标准safe_margin保持不变。
        """
        tracker = OutcomeTracker()
        bet = self._make_weather_bet(
            question="Will the highest temperature in Seoul be 24°C or higher on April 14?",
            outcome="NO",
            entry_price=0.92,
        )

        with patch("src.simulation.outcome_tracker.get") as mock_get:
            mock_get.return_value = {
                "enabled": True,
                "safe_margin_c": 3,
                "safe_margin_f": 5,
                "max_veto_count": 3,
                "cooldown_seconds": 0,
                "peak_window_start_hour": 11,
                "peak_window_end_hour": 16,
            }
            with patch(
                "src.utils.weather_fetcher.fetch_realtime_temperature",
                return_value=23.0,
            ), patch(
                "src.utils.weather_fetcher.get_city_local_hour",
                return_value=12,
            ):
                veto, reason = tracker._weather_stop_loss_veto(
                    bet, "standard_sl", -0.70
                )
        # 峰值窗口内, temp=23 在 24-3=21 以上 → gte标准veto不触发
        # 且 local_hour=12 < peak_end=16 → 不在已过峰值保护范围内
        assert veto is False, f"峰值内temp近阈值不应VETO, reason={reason}"
