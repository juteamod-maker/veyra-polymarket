"""
test_profit_taking.py — 止盈策略单元测试

2026-03-17 数据驱动重构：
  79笔止盈回测发现39笔quick_exit(<10%收益)仅赚7.84 USDC，
  若持有到结算潜在收益290.92 USDC — 机会成本283 USDC。
  二元市场核心：结果非0即1，小幅盈利退出=放弃巨大上行空间。

当前策略（保守持有为主，减少平仓次数）：
  1. Hold-to-Settlement: price>=0.90 + 距到期<48h → 持有到结算拿$1.00
  2. 天花板止盈（ceiling=0.95）：高确定性立即锁定
  3. 天气快速止盈（weather_quick_exit_price=0.65）：低价入场等高价
  4. Quick Exit: 已禁用（最大收益杀手）
  5. Standard TP: profit_take_pct=0.99（实际禁用）
  6. Trailing: min_profit=30%, drop=20%（大幅收紧，避免噪音）
  7. 临近到期: expiry_profit_pct=0.80（只锁高盈利）
"""

import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from src.simulation.outcome_tracker import OutcomeTracker
from src.utils import config_loader, db

# ------------------------------------------------------------------
# 测试夹具
# ------------------------------------------------------------------


@pytest.fixture
def no_dynamic_floor(monkeypatch):
    """禁用动态 trailing floor，隔离测试 trailing trigger 行为。"""
    _original_get = config_loader.get

    def _patched_get(key, *a, **kw):
        result = _original_get(key, *a, **kw)
        if key == "profit_taking" and isinstance(result, dict):
            result = {**result, "trailing_floor_dynamic_enabled": False}
        return result

    monkeypatch.setattr("src.simulation.outcome_tracker.get", _patched_get)


@pytest.fixture
def tmp_db(tmp_path: Path):
    """每个测试用独立的 SQLite，互不干扰。"""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    return db_path


def _make_bet(
    market_id: str = "mkt-001",
    entry_price: float = 0.39,
    outcome: str = "YES",
    size: float = 1.0,
    end_date: str = "2027-12-31T12:00:00Z",
) -> dict:
    """生成一条标准模拟投注记录（仿 Seoul 案例）。
    默认 end_date 设为远期日期，避免触发临近到期止盈逻辑干扰测试。
    """
    return {
        "bet_id": str(uuid.uuid4()),
        "market_id": market_id,
        "question": "Will the highest temperature in Seoul be 10°C on March 15?",
        "outcome": outcome,
        "market_price": entry_price,
        "size_usdc": size,
        "market_end_date": end_date,
        "ts_entered": time.time() - 3600,  # 1 小时前买入
    }


def _make_tracker(current_prices: dict) -> OutcomeTracker:
    """创建 OutcomeTracker，mock Gamma API 返回固定价格。
    bestBid = price，bestAsk = price + 0.01（模拟类真实市场 bid/ask 数据）。
    测试中 bestBid 设为目标价格，确保 _extract_sell_price 走真实代码路径。
    """
    tracker = OutcomeTracker()

    def fake_fetch(market_ids, **kwargs):
        result = {}
        for mid in market_ids:
            price = current_prices.get(mid, 0.5)
            # bestBid = price（卖出价）；bestAsk 略高（不影响止盈走 YES sell 路径）
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


class TestCeilingTake:
    """天花板止盈：二元市场价格≥0.99时立即卖出。
    T231: near_ceiling_threshold从0.95提高到0.99，与certainty_sell_threshold相同。
    因此 certainty_sell（优先级-1）先于 ceiling 触发。测试验证≥0.99时确实卖出。"""

    def test_ceiling_exit_seoul_case(self):
        """Seoul 真实案例：39¢买入，价格涨到99.2¢（≥0.99）→ certainty_sell触发（优先级高于ceiling）。"""
        bet = _make_bet(entry_price=0.39)
        tracker = _make_tracker({"mkt-001": 0.992})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "certainty_sell"
        assert exits[0]["exit_price"] == pytest.approx(0.992)

    def test_ceiling_exit_at_exact_threshold(self):
        """价格恰好等于阈值 0.99 → 应触发 certainty_sell（优先级-1）。"""
        bet = _make_bet(entry_price=0.39)
        tracker = _make_tracker({"mkt-001": 0.99})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "certainty_sell"

    def test_no_exit_just_below_ceiling(self):
        """价格 0.98（略低于阈值 0.99）且收益不达标 → 不触发任何止盈。"""
        # entry=0.92, price=0.98 → return=6.5%，不满足任何条件
        bet = _make_bet(entry_price=0.92)
        tracker = _make_tracker({"mkt-001": 0.98})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []

    def test_ceiling_fires_regardless_of_return_pct(self):
        """即使买入价很高（如 0.96），只要当前价≥0.99，仍触发卖出。"""
        # 收益率仅 3.1%，但价格已达阈值
        bet = _make_bet(entry_price=0.96)
        tracker = _make_tracker({"mkt-001": 0.99})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "certainty_sell"


# ------------------------------------------------------------------
# 2. 标准止盈（Standard Take）
# ------------------------------------------------------------------


class TestStandardTake:
    """标准止盈: profit_take_pct=0.99（实际禁用）。
    数据证明50%止盈放弃了大量resolution=1.0的收益。
    以下测试验证standard已禁用，以及trailing的新参数（min_profit=30%, drop=20%）。
    """

    def test_standard_profit_take_disabled_at_50_pct(self):
        """entry=0.34, price=0.52 → return=52.9%。旧系统触发standard，新系统不触发（99%阈值）。"""
        bet = _make_bet(entry_price=0.34, market_id="mkt-std")
        tracker = _make_tracker({"mkt-std": 0.52})
        exits = tracker.check_profit_take_exits([bet])
        assert (
            exits == []
        ), "standard_profit_take at 50% should NOT trigger (disabled at 99%)"

    def test_standard_profit_take_disabled_at_80_pct(self):
        """entry=0.40, price=0.72 → return=80%。即使80%收益也不触发standard（99%阈值）。"""
        bet = _make_bet(entry_price=0.40, market_id="mkt-80")
        tracker = _make_tracker({"mkt-80": 0.72})
        exits = tracker.check_profit_take_exits([bet])
        assert (
            exits == []
        ), "standard_profit_take at 80% should NOT trigger (disabled at 99%)"

    def test_no_standard_profit_take_below_threshold(self):
        """entry=0.40, price=0.55 → return=37.5% < 99% → 不触发。"""
        bet = _make_bet(entry_price=0.40, market_id="mkt-below")
        tracker = _make_tracker({"mkt-below": 0.55})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []

    def test_low_entry_no_trigger_with_quick_exit_disabled(self):
        """entry=0.10 < 0.25，quick_exit已禁用 → 不走quick_exit，也不走standard。
        price=0.16, return=60%, 但quick_exit disabled。weather: entry<=0.23, price=0.16<0.65。
        trailing: 首次调用创新高。无触发。"""
        bet = _make_bet(entry_price=0.10, market_id="mkt-low")
        tracker = _make_tracker({"mkt-low": 0.16})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == [], "quick_exit disabled + low entry → no trigger"

    def test_high_return_no_standard_trigger(self):
        """entry=0.25, price=0.40 → return=60%。standard disabled(99%)。trailing首次创新高。"""
        bet = _make_bet(entry_price=0.25, market_id="mkt-new")
        tracker = _make_tracker({"mkt-new": 0.40})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == [], "60% return should NOT trigger standard (disabled at 99%)"

    def test_trailing_exit_on_pullback_with_new_params(self, no_dynamic_floor):
        """Trailing新参数验证：min_profit=50%, drop=20%。
        entry=0.30, peak=0.59, drop to 0.46 → return=53.3%≥50%, drop=22.0%>20% → trailing 触发。
        floor=0.45 < 0.46 → floor_protection 不触发（dynamic_floor disabled）。
        standard: 96.7% < 99% → 不触发。
        """
        bet = _make_bet(entry_price=0.30, market_id="mkt-trail")
        tracker = _make_tracker({"mkt-trail": 0.59})
        # 第一轮: 价格0.59，创新高（return=96.7%），standard disabled(99%)
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []
        # 第二轮: 价格回撤到 0.46, drop=(0.59-0.46)/0.59=22.0%>20%, return=53.3%≥50%
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-trail": 0.46}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "trailing"

    def test_no_trailing_within_tolerance(self):
        """回撤幅度不足（<20%）→ 不触发。entry=0.30, peak=0.50, drop to 0.45(10%)。"""
        bet = _make_bet(entry_price=0.30, market_id="mkt-t2")
        tracker = _make_tracker({"mkt-t2": 0.50})
        # 第一轮: 创高点
        tracker.check_profit_take_exits([bet])
        # 第二轮: 微回撤 0.50→0.45 (10% < 20%)
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-t2": 0.45}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []

    def test_trailing_no_fire_when_return_below_min_profit(self):
        """收益低于trailing_min_profit_pct(30%) → 即使回撤大也不触发。
        entry=0.40, peak=0.48(20%), drop to 0.38 → return=-5% → 不触发。"""
        bet = _make_bet(entry_price=0.40, market_id="mkt-neg")
        # 先创高点 0.48
        tracker = _make_tracker({"mkt-neg": 0.48})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []
        # 价格跌到 0.38，drop_from_peak=(0.48-0.38)/0.48=20.8% > 20%
        # 但 return_pct=(0.38-0.40)/0.40=-5% < 30% 最低盈利
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-neg": 0.38}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert (
            exits == []
        ), "trailing should NOT fire when return_pct < trailing_min_profit_pct(30%)"

    def test_trailing_no_fire_at_breakeven(self):
        """T225: 价格创高后回到入场价 → floor_min_profit_pct=0.02 → floor=entry×1.02。
        价格=entry < floor → floor_protection 触发（赢家变输家保护）。"""
        bet = _make_bet(entry_price=0.30, market_id="mkt-be")
        tracker = _make_tracker({"mkt-be": 0.50})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []
        # 跌回入场价 → 低于 floor(0.306) → floor_protection 触发
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-be": 0.30}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "floor_protection"

    def test_floor_protection_skips_when_loss(self):
        """T227: 价格跌到entry以下(亏损) → floor_protection 不应触发。
        防止"止盈退出但亏损"的BUG。亏损仓位应由止损逻辑处理。"""
        bet = _make_bet(entry_price=0.30, market_id="mkt-loss")
        # 先让 peak 达到高位，触发 floor 创建
        tracker = _make_tracker({"mkt-loss": 0.50})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []  # 在创新高，不卖

        # 价格暴跌到 entry 以下（亏损）
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-loss": 0.25}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        # return_pct = (0.25-0.30)/0.30 = -16.7% < 0 → floor_protection 不应触发
        for e in exits:
            assert e["trigger"] != "floor_protection", (
                f"floor_protection should NOT fire when position is at a loss "
                f"(return={e.get('return_pct', 'N/A')})"
            )


# ------------------------------------------------------------------
# 2b. 天气快速止盈（Weather Quick Exit）
# ------------------------------------------------------------------


class TestWeatherQuickExit:
    """天气快速止盈：低价入场（entry ≤ 0.23）后价格到达0.65立即出场。
    从0.43提高到0.65——不在330%就退出，等更高确定性。"""

    def test_weather_quick_exit_triggers_at_65_cents(self):
        """10¢买入，价格涨到65¢ → 触发天气快速止盈（收益550%）。"""
        bet = _make_bet(entry_price=0.10)
        tracker = _make_tracker({"mkt-001": 0.65})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "weather_quick"
        assert exits[0]["exit_price"] == pytest.approx(0.65)

    def test_weather_quick_exit_no_fire_before_threshold(self):
        """entry=0.10, price=0.43 → 仅43¢ < 65¢目标 → 不触发天气快速止盈。
        旧系统(0.43目标)会触发，新系统等更高确定性。"""
        bet = _make_bet(entry_price=0.10)
        tracker = _make_tracker({"mkt-001": 0.43})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == [], "0.43 should NOT trigger weather_quick (new target=0.65)"

    def test_weather_quick_exit_not_triggered_for_high_entry(self):
        """高价入场（entry=0.39 > weather_quick_entry_max=0.23）不触发天气快速止盈。"""
        bet = _make_bet(entry_price=0.39)
        tracker = _make_tracker({"mkt-001": 0.65})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []

    def test_ceiling_takes_priority_over_weather_quick(self):
        """价格≥0.99时 certainty_sell 优先（优先级-1，高于 weather_quick 和 ceiling）。"""
        bet = _make_bet(entry_price=0.10)
        tracker = _make_tracker({"mkt-001": 0.99})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "certainty_sell"


# ------------------------------------------------------------------
# 3. 临近到期止盈
# ------------------------------------------------------------------


class TestExpiryTake:
    """临近到期止盈：到期<24h 且收益≥80%时触发。
    从50%提到80%——只有高盈利才在临期锁定，中等盈利继续持有等结算。"""

    def test_expiry_trigger_near_deadline_with_high_profit(self):
        """临近到期（<24h）且盈利≥80% → expiry 触发。
        standard disabled(99%), ceiling < 0.95, quick_exit disabled。"""
        import datetime

        expiry = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=12)
        ).isoformat()
        bet = _make_bet(entry_price=0.40, end_date=expiry)
        # 当前价 0.73 → 收益 = 82.5% ≥ 80%
        # hold-to-settle: 0.73 < 0.90 → 不触发
        tracker = _make_tracker({"mkt-001": 0.73})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "expiry"

    def test_expiry_no_trigger_at_50_pct_profit(self):
        """临近到期但盈利52%（旧系统触发，新系统不触发——需80%）。"""
        import datetime

        expiry = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=12)
        ).isoformat()
        bet = _make_bet(entry_price=0.50, end_date=expiry)
        # 当前价 0.76 → 收益 = 52% < 80%
        tracker = _make_tracker({"mkt-001": 0.76})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == [], "52% profit should NOT trigger expiry (new threshold=80%)"

    def test_no_expiry_trigger_if_not_profitable_enough(self):
        """临近到期但盈利仅20% → 不触发（远低于80%阈值）。"""
        import datetime

        expiry = (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=12)
        ).isoformat()
        bet = _make_bet(entry_price=0.50, end_date=expiry)
        # 当前价 0.60 → 收益 = 20% < 80%
        tracker = _make_tracker({"mkt-001": 0.60})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []


# ------------------------------------------------------------------
# 4. P&L 计算
# ------------------------------------------------------------------


class TestPnlCalc:
    def test_profit_take_pnl_seoul_case(self, tmp_db):
        """Seoul 真实案例：买入 1U@39¢，卖出@94¢，P&L 应约 +$1.41。"""
        bet = _make_bet(entry_price=0.39, size=1.0)
        bet["exit_price"] = 0.94
        bet["return_pct"] = (0.94 - 0.39) / 0.39

        tracker = OutcomeTracker()
        # 插入记录
        record = {
            **bet,
            "ts_entered": time.time(),
            "category": "weather",
            "confidence": 0.8,
            "ai_probability": 0.9,
            "ev": 0.05,
            "sim_balance_before": 100.0,
            "sim_balance_after": 99.0,
            "market_end_date": bet["market_end_date"],
            "weather_context": "",
            "ai_models": "gpt-4o",
            "cycle_id": 1,
        }
        db.insert_paper_bet(record, tmp_db)

        tracker.close_position_profit_take(bet, db_path=tmp_db)

        rows = db.get_paper_summary(tmp_db)
        # 应有 1 条已解决
        assert rows["resolved"] == 1
        assert rows["total_pnl"] == pytest.approx(1.41, abs=0.01)

    def test_profit_take_pnl_calculation(self, tmp_db):
        """通用 P&L 验证：shares = size/entry，proceeds = shares×exit。"""
        bet = _make_bet(entry_price=0.50, size=2.0)
        bet["exit_price"] = 0.80
        bet["return_pct"] = 0.60

        tracker = OutcomeTracker()
        record = {
            **bet,
            "ts_entered": time.time(),
            "category": "weather",
            "confidence": 0.7,
            "ai_probability": 0.8,
            "ev": 0.04,
            "sim_balance_before": 100.0,
            "sim_balance_after": 98.0,
            "market_end_date": bet["market_end_date"],
            "weather_context": "",
            "ai_models": "gpt-4o",
            "cycle_id": 1,
        }
        db.insert_paper_bet(record, tmp_db)
        tracker.close_position_profit_take(bet, db_path=tmp_db)

        # shares = 2.0/0.50 = 4.0; proceeds = 4.0×0.80 = 3.20; pnl = 3.20-2.00 = 1.20
        summary = db.get_paper_summary(tmp_db)
        assert summary["total_pnl"] == pytest.approx(1.20, abs=0.001)

    def test_exit_type_recorded_as_profit_take(self, tmp_db):
        """close_paper_bet_profit_take 应写入 exit_type='profit_take'。"""
        bet = _make_bet(entry_price=0.39, size=1.0)
        bet["exit_price"] = 0.94
        bet["return_pct"] = 1.41

        record = {
            **bet,
            "ts_entered": time.time(),
            "category": "weather",
            "confidence": 0.9,
            "ai_probability": 0.95,
            "ev": 0.06,
            "sim_balance_before": 100.0,
            "sim_balance_after": 99.0,
            "market_end_date": bet["market_end_date"],
            "weather_context": "",
            "ai_models": "gpt-4o",
            "cycle_id": 1,
        }
        db.insert_paper_bet(record, tmp_db)
        db.close_paper_bet_profit_take(
            bet_id=bet["bet_id"], exit_price=0.94, pnl=1.41, payout=2.41, db_path=tmp_db
        )

        with db.connect(tmp_db) as conn:
            row = conn.execute(
                "SELECT exit_type, resolved_outcome FROM paper_bets WHERE bet_id=?",
                (bet["bet_id"],),
            ).fetchone()
        assert row["exit_type"] == "profit_take"
        assert row["resolved_outcome"] == "PROFIT_TAKE"


# ------------------------------------------------------------------
# 5. 禁用状态
# ------------------------------------------------------------------


class TestDisabledProfitTake:
    def test_disabled_returns_empty(self):
        """profit_taking.enabled=false 时，任何仓位都不触发止盈（包括天花板价格）。"""
        bet = _make_bet(entry_price=0.39)
        tracker = _make_tracker({"mkt-001": 0.99})

        with patch(
            "src.simulation.outcome_tracker.get",
            return_value={
                "enabled": False,
                "near_ceiling_threshold": 0.97,
                "profit_take_pct": 0.80,
                "expiry_hours_threshold": 24,
                "expiry_profit_pct": 0.50,
            },
        ):
            exits = tracker.check_profit_take_exits([bet])
        assert exits == []


# ------------------------------------------------------------------
# 6. 体育比赛进行中（Sports Live）止盈
# ------------------------------------------------------------------


class TestSportsLiveProfitTake:
    """
    体育比赛进行中(距到期<4h)止盈参数更新：
      - near_ceiling_threshold: 0.93
      - profit_take_pct: 0.99（禁用，与全局策略对齐）
      - hold_to_settle: 0.90 会覆盖 live_ceiling（因为 0.93 > 0.90）

    注意：hold-to-settle(0.90)在live sports完全覆盖live_ceiling(0.93)，
    因为is_live要求hours_to_expiry<4h，而hold要求<48h。
    任何 price>=0.93 也 >=0.90，所以hold总是优先。
    """

    def _make_live_sports_bet(self, entry_price=0.50, hours_ahead=2.0):
        """生成距到期 hours_ahead 小时的体育 bet。"""
        from datetime import datetime, timedelta, timezone

        end_dt = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
        return {
            "bet_id": str(__import__("uuid").uuid4()),
            "market_id": "mkt-001",
            "question": "Lakers vs. Celtics",
            "outcome": "YES",
            "market_price": entry_price,
            "size_usdc": 1.0,
            "market_end_date": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ts_entered": __import__("time").time() - 3600,
            "category": "sports",
        }

    def test_hold_to_settle_overrides_live_ceiling(self):
        """比赛中价格0.94: >=live_ceiling(0.93) 但也 >=hold(0.90) + 近到期
        → hold-to-settle优先，不触发止盈（持有到结算拿$1.00）。"""
        bet = self._make_live_sports_bet(entry_price=0.50, hours_ahead=2.0)
        tracker = _make_tracker({"mkt-001": 0.94})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == [], "hold-to-settle(0.90) should override live_ceiling(0.93)"

    def test_live_no_standard_profit_at_40_pct(self):
        """比赛中盈利44%: live_profit_pct=0.99(禁用) → 不触发。
        旧系统(0.40阈值)会触发live_standard，新系统持有等结算。"""
        bet = self._make_live_sports_bet(entry_price=0.50, hours_ahead=1.5)
        tracker = _make_tracker({"mkt-001": 0.72})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == [], "live_standard disabled (0.99 threshold)"

    def test_pregame_sports_uses_global_ceiling(self):
        """到期>4h（非live）→ 用全局阈值 0.99。
        entry=0.50, price=0.99 → certainty_sell 触发。hours=72 > 48 → hold不触发。"""
        bet = self._make_live_sports_bet(entry_price=0.50, hours_ahead=72.0)
        tracker = _make_tracker({"mkt-001": 0.99})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "certainty_sell"

    def test_live_no_profit_within_tolerance(self):
        """比赛中盈利 +20% → 不触发（hold/ceiling/standard 均不满足）。"""
        bet = self._make_live_sports_bet(entry_price=0.50, hours_ahead=2.0)
        tracker = _make_tracker({"mkt-001": 0.60})  # +20%
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []


# ------------------------------------------------------------------
# 7. Hold-to-Settlement（高概率持有到结算）
# ------------------------------------------------------------------


class TestHoldToSettlement:
    """高概率 + 临近到期 → 持有到结算拿 $1.00，跳过所有止盈。
    阈值从0.95降到0.90，更宽泛地持有高概率仓位到结算。"""

    def _make_expiry_bet(self, entry_price=0.50, hours_ahead=12.0):
        from datetime import datetime, timedelta, timezone

        end_dt = datetime.now(timezone.utc) + timedelta(hours=hours_ahead)
        return {
            "bet_id": str(__import__("uuid").uuid4()),
            "market_id": "mkt-hold",
            "question": "NBA Portland vs Philadelphia",
            "outcome": "YES",
            "market_price": entry_price,
            "size_usdc": 1.0,
            "market_end_date": end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ts_entered": __import__("time").time() - 3600,
            "category": "sports",
        }

    def test_hold_to_settle_at_91_pct_near_expiry(self):
        """概率0.91 >= 0.90，距到期12h < 48h → 持有到结算，不触发止盈。"""
        bet = self._make_expiry_bet(entry_price=0.50, hours_ahead=12.0)
        tracker = _make_tracker({"mkt-hold": 0.91})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []

    def test_hold_to_settle_overrides_ceiling(self):
        """价格0.98 >= ceiling 0.95，但也 >= hold 0.90 + 到期近 → 持有到结算优先。"""
        bet = self._make_expiry_bet(entry_price=0.50, hours_ahead=6.0)
        tracker = _make_tracker({"mkt-hold": 0.98})
        exits = tracker.check_profit_take_exits([bet])
        # hold-to-settle 优先级最高，跳过所有止盈
        assert exits == []

    def test_no_hold_if_far_from_expiry(self):
        """概率0.99，但距到期72h > 48h → 不适用 hold-to-settle。
        0.99 >= certainty_sell 0.99 → certainty_sell 触发。"""
        bet = self._make_expiry_bet(entry_price=0.50, hours_ahead=72.0)
        tracker = _make_tracker({"mkt-hold": 0.99})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "certainty_sell"

    def test_no_hold_if_probability_below_threshold(self):
        """概率0.89 < 0.90 → 不走 hold-to-settle。
        距到期12h < 48h，但价格低于hold阈值。
        0.89 < 0.95 ceiling → 不触发ceiling。
        return=78% < 99% → 不触发standard。无触发。"""
        bet = self._make_expiry_bet(entry_price=0.50, hours_ahead=12.0)
        tracker = _make_tracker({"mkt-hold": 0.89})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == [], "0.89 < hold_threshold(0.90) → normal flow, no trigger"


# ------------------------------------------------------------------
# 8. Trailing Take-Profit（追踪止赢）
# ------------------------------------------------------------------


class TestTrailingTakeProfit:
    """追踪止盈：从历史最高价回撤 N% 才卖出，让利润奔跑。
    新参数：min_profit=30%, drop=20%（大幅收紧避免噪音震出）。"""

    def test_trailing_records_peak_and_triggers_on_drop(self, no_dynamic_floor):
        """两轮调用：第一轮创新高，第二轮回撤>20% + return≥50% → trailing 触发。
        entry=0.30, peak=0.59, drop to 0.46 → drop=22.0%, return=53.3% → 触发。
        floor=0.45 < 0.46 → floor_protection 不触发（dynamic_floor disabled）。"""
        bet = _make_bet(entry_price=0.30, market_id="mkt-t")
        tracker = _make_tracker({"mkt-t": 0.59})

        # 第一轮：价格0.59，创新高，不触发
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []

        # 第二轮：价格回撤到0.46，drop=(0.59-0.46)/0.59=22.0%>20%，return=53.3%≥50%
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-t": 0.46}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "trailing"

    def test_trailing_no_trigger_while_rising(self):
        """价格连续上涨但standard disabled → 持续更新 peak，不触发。"""
        bet = _make_bet(entry_price=0.40, market_id="mkt-r")
        tracker = _make_tracker({"mkt-r": 0.48})

        tracker.check_profit_take_exits([bet])
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-r": 0.52}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []

        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-r": 0.56}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []

    def test_ceiling_still_fires_even_with_trailing(self):
        """价格到0.99 >= certainty_sell 0.99 → certainty_sell 优先，不等 trailing。"""
        bet = _make_bet(entry_price=0.30)
        tracker = _make_tracker({"mkt-001": 0.99})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "certainty_sell"

    def test_peak_cleanup_on_closed_positions(self):
        """已平仓的 peak 记录应被清理。"""
        bet1 = _make_bet(entry_price=0.35, market_id="mkt-c")
        bet2 = _make_bet(entry_price=0.35, market_id="mkt-d")
        tracker = _make_tracker({"mkt-c": 0.45, "mkt-d": 0.45})
        tracker.check_profit_take_exits([bet1, bet2])
        assert len(tracker._peak_prices) >= 2
        # 只传入 bet2 → bet1 的 peak 应被清理
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-d": 0.45}
        )._fetch_market_statuses
        tracker.check_profit_take_exits([bet2])
        # bet1 的 peak 应已清理
        peak_keys = set(tracker._peak_prices.keys())
        assert bet1["bet_id"] not in peak_keys


# ------------------------------------------------------------------
# 8b. 阶梯 Trailing + 天气最低回调保护
# ------------------------------------------------------------------


class TestTieredTrailing:
    """阶梯 Trailing: 收益越高允许越多回调空间。

    新配置 trailing_tiers:
      return < 60%: drop=20%（默认）
      return 60-100%: drop=25%（大赢给更多空间）
      return > 100%: drop=20%（超高收益适度保护）
    trailing_min_profit_pct: 30%（只有确认大幅盈利才跟踪）
    天气市场最低回调: 15%（从10%提高）
    """

    def test_mid_return_uses_20pct_drop_tier(self, no_dynamic_floor):
        """return=53.3%(在30-60%区间), peak=0.59, drop to 0.46 → drop=22.0% > tier(20%) → 触发。
        floor=0.45 < 0.46 → floor_protection 不触发（dynamic_floor disabled）。"""
        bet = _make_bet(entry_price=0.30, market_id="mkt-tier1")
        tracker = _make_tracker({"mkt-tier1": 0.59})
        # Round 1: create peak at 0.59
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []
        # Round 2: drop to 0.46, drop=(0.59-0.46)/0.59=22.0%>20%, return=53.3%≥50%
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-tier1": 0.46}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "trailing"

    def test_mid_return_no_trigger_at_15pct_drop(self):
        """return~53%, tier drop=20%, actual drop=14% → 不触发（14% < 20%）。
        entry=0.30, peak=0.56, drop to 0.48 → return=60%, drop=14.3% < 20%。
        floor=0.45 < 0.48 → floor_protection 不触发。"""
        bet = _make_bet(entry_price=0.30, market_id="mkt-tier2")
        tracker = _make_tracker({"mkt-tier2": 0.56})
        tracker.check_profit_take_exits([bet])
        # drop to 0.48: drop=(0.56-0.48)/0.56=14.3%, return=(0.48-0.30)/0.30=60%
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-tier2": 0.48}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert exits == [], "14% drop should NOT trigger at return<60% (needs 20%)"

    def test_high_return_uses_25pct_drop_tier(self, no_dynamic_floor):
        """return=66.7%(60-100%区间), tier drop=25%, actual drop=33% → 触发。
        entry=0.24 (< 0.25避免standard检查), peak=0.60, drop to 0.40。"""
        bet = _make_bet(entry_price=0.24, market_id="mkt-tier3")
        tracker = _make_tracker({"mkt-tier3": 0.60})
        tracker.check_profit_take_exits([bet])
        # drop to 0.40: drop=(0.60-0.40)/0.60=33.3%, return=(0.40-0.24)/0.24=66.7%
        # tier: 60-100% → drop=25%. 33.3% ≥ 25% → triggers
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-tier3": 0.40}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "trailing"

    def test_high_return_no_trigger_at_20pct_drop(self):
        """return>100%, tier=20%, actual drop=18% → 不触发（18% < 20%）。
        entry=0.24, peak=0.60, drop to 0.492 → return=105%, drop=18%。"""
        bet = _make_bet(entry_price=0.24, market_id="mkt-tier3b")
        tracker = _make_tracker({"mkt-tier3b": 0.60})
        tracker.check_profit_take_exits([bet])
        # drop to 0.492: drop=(0.60-0.492)/0.60=18%, return=(0.492-0.24)/0.24=105%
        # tier: >100% → drop=20%. 18% < 20% → no trigger
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-tier3b": 0.492}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet])
        assert exits == [], "18% drop should NOT trigger at >100% return (needs 20%)"

    def test_weather_trailing_with_15pct_min_drop(self):
        """天气市场: weather_trailing_min_drop=45%。
        entry=0.30, peak=0.56, drop 14% → 不触发 (max(tier20%, weather45%)=45%)。
        floor=0.45 < 0.49 → floor_protection 不触发。"""
        bet_weather = _make_bet(entry_price=0.30, market_id="mkt-w")
        bet_weather["category"] = "weather"
        tracker = _make_tracker({"mkt-w": 0.56})
        tracker.check_profit_take_exits([bet_weather])
        # drop to 0.49: drop=(0.56-0.49)/0.56=12.5%, return=(0.49-0.30)/0.30=63.3%
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-w": 0.49}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet_weather])
        assert (
            exits == []
        ), "12.5% drop should NOT trigger weather trailing (eff=max(25%,45%)=45%)"

    def test_weather_trailing_triggers_above_45pct(self, no_dynamic_floor):
        """天气市场: entry=0.30, peak=0.85, drop 47% → 触发 (eff=max(20%,45%)=45%, 47%>45%)。
        T262: weather_trailing_min_drop 从0.25上调至0.45，需要更大跌幅才触发。"""
        bet_weather = _make_bet(entry_price=0.30, market_id="mkt-w2")
        bet_weather["category"] = "weather"
        tracker = _make_tracker({"mkt-w2": 0.85})
        tracker.check_profit_take_exits([bet_weather])
        # drop to 0.45: drop=(0.85-0.45)/0.85=47.1%>45%, return=(0.45-0.30)/0.30=50%≥40%
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-w2": 0.45}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet_weather])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "trailing"

    def test_non_weather_same_20pct_default_tier(self, no_dynamic_floor):
        """非天气市场: entry=0.30, peak=0.59, drop 22% → 触发（tier=20%）。
        floor=0.45 < 0.46 → floor_protection 不触发（dynamic_floor disabled）。
        注意: 使用 geopolitics 而非 crypto（crypto有专属覆盖参数）。"""
        bet_geo = _make_bet(entry_price=0.30, market_id="mkt-c")
        bet_geo["category"] = "geopolitics"
        tracker = _make_tracker({"mkt-c": 0.59})
        tracker.check_profit_take_exits([bet_geo])
        # drop to 0.46: drop=(0.59-0.46)/0.59=22.0%>20%, return=53.3%≥50%
        tracker._fetch_market_statuses = _make_tracker(
            {"mkt-c": 0.46}
        )._fetch_market_statuses
        exits = tracker.check_profit_take_exits([bet_geo])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "trailing"


# ------------------------------------------------------------------
# 6. Quick Exit 快速套利止盈
# ------------------------------------------------------------------


class TestQuickExit:
    """Quick Exit: 已禁用（quick_exit_enabled=false）。
    数据证明这是最大的收益杀手：39笔仅赚7.84 USDC，放弃283 USDC机会成本。
    以下测试验证 quick_exit 在任何条件下都不再触发。
    """

    def test_quick_exit_disabled_taiwan_case(self):
        """Taiwan 案例：entry=0.106，price=0.113（+6.6%）→ 旧系统触发quick_exit，新系统不触发。"""
        bet = _make_bet(entry_price=0.106, market_id="mkt-taiwan")
        tracker = _make_tracker({"mkt-taiwan": 0.113})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == [], "quick_exit disabled → Taiwan case should NOT trigger"

    def test_quick_exit_disabled_above_threshold(self):
        """entry=0.20，price=0.215（+7.5%）→ 旧系统触发，新系统不触发。"""
        bet = _make_bet(entry_price=0.20, market_id="mkt-above")
        tracker = _make_tracker({"mkt-above": 0.215})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == [], "quick_exit disabled → should NOT trigger"

    def test_quick_exit_disabled_no_trigger_insufficient_profit(self):
        """entry=0.10，price=0.103（+3%）→ 不触发（连旧系统也不触发）。"""
        bet = _make_bet(entry_price=0.10, market_id="mkt-small")
        tracker = _make_tracker({"mkt-small": 0.103})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []

    def test_quick_exit_disabled_no_trigger_high_entry(self):
        """entry=0.50（> entry_max=0.25），即使有10%利润也不触发。"""
        bet = _make_bet(entry_price=0.50, market_id="mkt-high")
        tracker = _make_tracker({"mkt-high": 0.55})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []

    def test_quick_exit_disabled_no_trigger_at_loss(self):
        """entry=0.10，price=0.08（亏损20%）→ 不触发。"""
        bet = _make_bet(entry_price=0.10, market_id="mkt-loss")
        tracker = _make_tracker({"mkt-loss": 0.08})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []

    def test_ceiling_still_fires_for_low_entry(self):
        """entry=0.10，price=0.99（≥0.99）→ certainty_sell 触发（优先级高于 ceiling/weather_quick）。"""
        bet = _make_bet(entry_price=0.10, market_id="mkt-ceil")
        tracker = _make_tracker({"mkt-ceil": 0.99})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "certainty_sell"

    def test_weather_quick_still_fires_at_new_threshold(self):
        """entry=0.10，price=0.65（天气快速止盈新阈值）→ weather_quick触发。
        quick_exit disabled 不影响 weather_quick 路径。"""
        bet = _make_bet(entry_price=0.10, market_id="mkt-wq")
        tracker = _make_tracker({"mkt-wq": 0.65})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "weather_quick"

    def test_quick_exit_entry_boundary_still_no_trigger(self):
        """entry=0.25（= entry_max，条件是 < 0.25）→ 不触发（独立于disable状态）。"""
        bet = _make_bet(entry_price=0.25, market_id="mkt-bound")
        tracker = _make_tracker({"mkt-bound": 0.30})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []


# ------------------------------------------------------------------
# 7. Legacy Cleanup（方向错误遗留仓位清理）
# ------------------------------------------------------------------


class TestLegacyCleanup:
    """Legacy Cleanup：方向bug遗留仓位（ai_prob<0.5 but YES）任何正收益立即离场。

    真实案例：Taiwan invasion bet 66a65f30，ai_prob=0.125 买了 YES@0.106，
    价格涨到 0.108（+1.89%）但不够 quick_exit 的 5%。legacy_cleanup 在任何正收益就走。
    """

    def test_legacy_cleanup_taiwan_case(self):
        """ai_prob=0.125, outcome=YES, entry=0.106, price=0.108 (+1.89%) → legacy_cleanup。"""
        bet = _make_bet(entry_price=0.106, market_id="mkt-tw")
        bet["ai_probability"] = 0.125
        bet["outcome"] = "YES"
        tracker = _make_tracker({"mkt-tw": 0.108})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "legacy_cleanup"

    def test_legacy_cleanup_no_direction(self):
        """ai_prob=0.30, outcome=NO（方向错误：ai说YES但买了NO）有正收益 → legacy_cleanup。"""
        bet = _make_bet(entry_price=0.50, market_id="mkt-no")
        bet["ai_probability"] = 0.70  # P(YES)=0.70 > 0.5, but outcome=NO
        bet["outcome"] = "NO"
        # NO token price = bestBid for NO = 1 - bestAsk
        # maker: current bestAsk=0.52, NO sell price = 1-0.52 = 0.48... tricky
        # _make_tracker sets bestBid=price, so for YES sell it's price
        # For NO, _extract_sell_price uses 1 - bestAsk
        # bestAsk = price + 0.01, so NO sell price = 1 - (price+0.01)
        # We need NO sell price > 0.50 (entry), so 1 - (price+0.01) > 0.50 → price < 0.49
        tracker = _make_tracker({"mkt-no": 0.48})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "legacy_cleanup"

    def test_no_legacy_cleanup_correct_direction(self):
        """ai_prob=0.75, outcome=YES（方向正确）→ 不触发 legacy_cleanup。"""
        bet = _make_bet(entry_price=0.50, market_id="mkt-ok")
        bet["ai_probability"] = 0.75
        bet["outcome"] = "YES"
        tracker = _make_tracker({"mkt-ok": 0.55})
        exits = tracker.check_profit_take_exits([bet])
        # 正确方向仓位不会触发 legacy_cleanup
        # 也不触发 quick_exit（entry=0.50 > 0.25），不触发 trailing（新高）
        assert exits == []

    def test_no_legacy_cleanup_at_loss(self):
        """方向错误但亏损中 → 不触发 legacy_cleanup（等止损处理）。"""
        bet = _make_bet(entry_price=0.106, market_id="mkt-loss")
        bet["ai_probability"] = 0.125
        bet["outcome"] = "YES"
        tracker = _make_tracker({"mkt-loss": 0.10})
        exits = tracker.check_profit_take_exits([bet])
        assert exits == []

    def test_no_legacy_cleanup_missing_ai_prob(self):
        """ai_probability 字段缺失 → 不触发 legacy_cleanup。"""
        bet = _make_bet(entry_price=0.10, market_id="mkt-miss")
        # 不设置 ai_probability
        tracker = _make_tracker({"mkt-miss": 0.12})
        exits = tracker.check_profit_take_exits([bet])
        # 没有 ai_probability → legacy_cleanup 不触发
        # quick_exit disabled, weather_quick: entry=0.10<=0.23 but price=0.12<0.65 → no trigger
        if exits:
            assert exits[0]["trigger"] != "legacy_cleanup"

    def test_no_legacy_cleanup_borderline_no_bet(self):
        """Phase29修复: ai_prob=0.57 + outcome=NO → 0.57 < 0.60 threshold → 不触发legacy_cleanup。
        76ers bet被误杀案例：ensemble决策可合法偏移至0.50-0.60区间。
        """
        bet = _make_bet(entry_price=0.341, market_id="mkt-76ers")
        bet["ai_probability"] = 0.57  # P(YES)=0.57, borderline
        bet["outcome"] = "NO"
        # NO sell price = 1 - bestAsk = 1 - 0.66 = 0.34 → need > 0.341 for profit
        # use YES price 0.64 → bestAsk=0.65 → NO sell = 0.35 → return +2.6%
        tracker = _make_tracker({"mkt-76ers": 0.64})
        exits = tracker.check_profit_take_exits([bet])
        # Should NOT trigger legacy_cleanup because 0.57 < 0.60 threshold
        for e in exits:
            assert e["trigger"] != "legacy_cleanup"

    def test_legacy_cleanup_still_fires_above_threshold(self):
        """ai_prob=0.65 + outcome=NO → 0.65 > 0.60 threshold → 仍触发legacy_cleanup。"""
        bet = _make_bet(entry_price=0.50, market_id="mkt-clear-wrong")
        bet["ai_probability"] = 0.65  # P(YES)=0.65, clearly wrong direction for NO
        bet["outcome"] = "NO"
        # NO sell price = 1 - bestAsk = 1 - 0.49 = 0.51 → profit from 0.50
        tracker = _make_tracker({"mkt-clear-wrong": 0.48})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "legacy_cleanup"

    def test_weather_no_immune_to_legacy_cleanup(self):
        """T188修复: 天气NO仓位 ai_prob=0.99 不触发 legacy_cleanup。

        天气pipeline的 ai_probability 表示 P(NO-side wins)，不是 P(YES)。
        Seoul 19°C NO bet: GFS=0.000, ECMWF=0.020 → ai_prob=0.99
        LegacyCleanup误读为"AI说YES=0.99却买NO=方向错误" → 误杀。
        """
        bet = _make_bet(entry_price=0.81, market_id="mkt-seoul")
        bet["ai_probability"] = 0.99  # P(NO wins) in weather pipeline
        bet["outcome"] = "NO"
        bet["category"] = "weather"
        bet["question"] = "Will the highest temperature in Seoul be 19°C on April 12?"
        # NO sell price > entry → positive return → would trigger old cleanup
        tracker = _make_tracker({"mkt-seoul": 0.17})  # YES=0.17 → NO≈0.82
        exits = tracker.check_profit_take_exits([bet])
        # Must NOT trigger legacy_cleanup — weather is immune
        for e in exits:
            assert e["trigger"] != "legacy_cleanup"

    def test_weather_yes_immune_to_legacy_cleanup(self):
        """天气YES仓位 ai_prob=0.10 也不触发 legacy_cleanup。"""
        bet = _make_bet(entry_price=0.10, market_id="mkt-wx-yes")
        bet["ai_probability"] = 0.10
        bet["outcome"] = "YES"
        bet["category"] = "weather"
        tracker = _make_tracker({"mkt-wx-yes": 0.12})
        exits = tracker.check_profit_take_exits([bet])
        for e in exits:
            assert e["trigger"] != "legacy_cleanup"

    def test_non_weather_still_triggers_legacy_cleanup(self):
        """Non-weather NO bet with ai_prob=0.99 → still triggers legacy_cleanup。"""
        bet = _make_bet(entry_price=0.50, market_id="mkt-politics")
        bet["ai_probability"] = 0.99
        bet["outcome"] = "NO"
        bet["category"] = "politics"
        tracker = _make_tracker({"mkt-politics": 0.48})  # NO sell=0.51 >0.50
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "legacy_cleanup"


# ------------------------------------------------------------------
# Crypto 赛道专用止盈覆盖测试 (T41)
# ------------------------------------------------------------------


def _make_crypto_bet(
    market_id: str = "mkt-crypto-001",
    entry_price: float = 0.40,
    outcome: str = "YES",
    size: float = 1.0,
    end_date: str = "2027-12-31T12:00:00Z",
    hours_ago: float = 1.0,
) -> dict:
    """生成一条 Crypto 赛道投注记录。"""
    return {
        "bet_id": str(uuid.uuid4()),
        "market_id": market_id,
        "question": "Will Bitcoin reach $100,000 by March 2026?",
        "outcome": outcome,
        "market_price": entry_price,
        "size_usdc": size,
        "market_end_date": end_date,
        "ts_entered": time.time() - hours_ago * 3600,
        "category": "crypto",
    }


class TestCryptoOverride:
    """Crypto 赛道专用止盈参数覆盖 (T41)：
    - certainty_sell: 0.95 (vs 标准 0.99)
    - near_ceiling: 0.93 (vs 标准 0.97)
    - profit_take_pct: 0.30 (vs 标准 0.50)
    - hold_to_settle: 0.93/24h (vs 标准 0.95/48h)
    - trailing: drop 0.15 / min_profit 0.10 (vs 0.08/0.05)
    - max_hold: 72h 限时止盈
    """

    @patch("src.simulation.outcome_tracker.get")
    def test_crypto_certainty_sell_lower_threshold(self, mock_get):
        """Crypto 价格 0.96 应触发 certainty_sell (阈值 0.95)，
        但同样价格对非 crypto 不应触发 (标准阈值 0.99)。"""
        _cfg = {
            "profit_taking": {
                "enabled": True,
                "certainty_sell_threshold": 0.99,
                "crypto": {
                    "override_enabled": True,
                    "certainty_sell_threshold": 0.95,
                    "near_ceiling_threshold": 0.93,
                    "profit_take_pct": 0.30,
                    "trailing_drop_pct": 0.15,
                    "trailing_min_profit_pct": 0.10,
                    "hold_to_settle_threshold": 0.93,
                    "hold_to_settle_hours": 24.0,
                    "max_hold_hours": 72.0,
                },
                "near_ceiling_threshold": 0.97,
                "trailing_enabled": True,
                "trailing_drop_pct": 0.08,
                "trailing_min_profit_pct": 0.05,
                "hold_to_settle_threshold": 0.95,
                "hold_to_settle_hours": 48.0,
                "profit_take_pct": 0.50,
                "quick_exit_enabled": False,
            },
            "stop_loss": {},
            "sports_live": {},
        }
        mock_get.side_effect = lambda k, *a: _cfg.get(k, {})

        bet = _make_crypto_bet(entry_price=0.40)
        tracker = _make_tracker({"mkt-crypto-001": 0.96})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "certainty_sell"

    @patch("src.simulation.outcome_tracker.get")
    def test_non_crypto_no_certainty_at_096(self, mock_get):
        """非 Crypto 价格 0.96 不应触发 certainty_sell (标准阈值 0.99)。"""
        _cfg = {
            "profit_taking": {
                "enabled": True,
                "certainty_sell_threshold": 0.99,
                "crypto": {
                    "override_enabled": True,
                    "certainty_sell_threshold": 0.95,
                    "near_ceiling_threshold": 0.93,
                    "profit_take_pct": 0.30,
                    "trailing_drop_pct": 0.15,
                    "trailing_min_profit_pct": 0.10,
                    "hold_to_settle_threshold": 0.93,
                    "hold_to_settle_hours": 24.0,
                    "max_hold_hours": 72.0,
                },
                "near_ceiling_threshold": 0.97,
                "trailing_enabled": True,
                "trailing_drop_pct": 0.08,
                "trailing_min_profit_pct": 0.05,
                "hold_to_settle_threshold": 0.95,
                "hold_to_settle_hours": 48.0,
                "profit_take_pct": 0.50,
                "quick_exit_enabled": False,
            },
            "stop_loss": {},
            "sports_live": {},
        }
        mock_get.side_effect = lambda k, *a: _cfg.get(k, {})

        # Weather bet at 0.96 — should NOT trigger certainty_sell
        bet = _make_bet(entry_price=0.40)
        bet["category"] = "weather"
        tracker = _make_tracker({"mkt-001": 0.96})
        exits = tracker.check_profit_take_exits([bet])
        certainty_exits = [e for e in exits if e.get("trigger") == "certainty_sell"]
        assert len(certainty_exits) == 0

    @patch("src.simulation.outcome_tracker.get")
    def test_crypto_ceiling_at_094(self, mock_get):
        """Crypto 价格 0.94 应触发 ceiling (crypto 阈值 0.93)。"""
        _cfg = {
            "profit_taking": {
                "enabled": True,
                "certainty_sell_threshold": 0.99,
                "crypto": {
                    "override_enabled": True,
                    "certainty_sell_threshold": 0.95,
                    "near_ceiling_threshold": 0.93,
                    "profit_take_pct": 0.30,
                    "trailing_drop_pct": 0.15,
                    "trailing_min_profit_pct": 0.10,
                    "hold_to_settle_threshold": 0.93,
                    "hold_to_settle_hours": 24.0,
                    "max_hold_hours": 72.0,
                },
                "near_ceiling_threshold": 0.97,
                "trailing_enabled": True,
                "trailing_drop_pct": 0.08,
                "trailing_min_profit_pct": 0.05,
                "hold_to_settle_threshold": 0.95,
                "hold_to_settle_hours": 48.0,
                "profit_take_pct": 0.50,
                "quick_exit_enabled": False,
            },
            "stop_loss": {},
            "sports_live": {},
        }
        mock_get.side_effect = lambda k, *a: _cfg.get(k, {})

        bet = _make_crypto_bet(entry_price=0.40)
        tracker = _make_tracker({"mkt-crypto-001": 0.94})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "ceiling"

    @patch("src.simulation.outcome_tracker.get")
    def test_crypto_standard_profit_take_30pct(self, mock_get):
        """Crypto entry=0.50, price=0.66 → return=32% ≥ 30% → standard_profit_take。
        标准赛道同条件 32% < 50% 不应触发。"""
        _cfg = {
            "profit_taking": {
                "enabled": True,
                "certainty_sell_threshold": 0.99,
                "crypto": {
                    "override_enabled": True,
                    "certainty_sell_threshold": 0.95,
                    "near_ceiling_threshold": 0.93,
                    "profit_take_pct": 0.30,
                    "trailing_drop_pct": 0.15,
                    "trailing_min_profit_pct": 0.10,
                    "hold_to_settle_threshold": 0.93,
                    "hold_to_settle_hours": 24.0,
                    "max_hold_hours": 72.0,
                },
                "near_ceiling_threshold": 0.97,
                "trailing_enabled": True,
                "trailing_drop_pct": 0.08,
                "trailing_min_profit_pct": 0.05,
                "hold_to_settle_threshold": 0.95,
                "hold_to_settle_hours": 48.0,
                "profit_take_pct": 0.50,
                "quick_exit_enabled": False,
                "quick_exit_entry_max": 0.25,
            },
            "stop_loss": {},
            "sports_live": {},
        }
        mock_get.side_effect = lambda k, *a: _cfg.get(k, {})

        # Crypto: entry=0.50, price=0.66, return=32% ≥ 30% → trigger
        bet = _make_crypto_bet(entry_price=0.50, market_id="mkt-c1")
        tracker = _make_tracker({"mkt-c1": 0.66})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "standard_profit_take"

    @patch("src.simulation.outcome_tracker.get")
    def test_non_crypto_no_standard_pt_at_32pct(self, mock_get):
        """非 Crypto entry=0.50, return=32% < 50% → 不触发 standard_profit_take。"""
        _cfg = {
            "profit_taking": {
                "enabled": True,
                "certainty_sell_threshold": 0.99,
                "crypto": {
                    "override_enabled": True,
                    "certainty_sell_threshold": 0.95,
                    "near_ceiling_threshold": 0.93,
                    "profit_take_pct": 0.30,
                    "trailing_drop_pct": 0.15,
                    "trailing_min_profit_pct": 0.10,
                    "hold_to_settle_threshold": 0.93,
                    "hold_to_settle_hours": 24.0,
                    "max_hold_hours": 72.0,
                },
                "near_ceiling_threshold": 0.97,
                "trailing_enabled": True,
                "trailing_drop_pct": 0.08,
                "trailing_min_profit_pct": 0.05,
                "hold_to_settle_threshold": 0.95,
                "hold_to_settle_hours": 48.0,
                "profit_take_pct": 0.50,
                "quick_exit_enabled": False,
                "quick_exit_entry_max": 0.25,
            },
            "stop_loss": {},
            "sports_live": {},
        }
        mock_get.side_effect = lambda k, *a: _cfg.get(k, {})

        bet = _make_bet(entry_price=0.50, market_id="mkt-w1")
        bet["category"] = "weather"
        tracker = _make_tracker({"mkt-w1": 0.66})
        exits = tracker.check_profit_take_exits([bet])
        # 32% < 50% → no standard_profit_take
        spt_exits = [e for e in exits if e.get("trigger") == "standard_profit_take"]
        assert len(spt_exits) == 0

    @patch("src.simulation.outcome_tracker.get")
    def test_crypto_max_hold_hours_trigger(self, mock_get):
        """Crypto 持仓超过 72h 且有正收益 → crypto_max_hold 触发。"""
        _cfg = {
            "profit_taking": {
                "enabled": True,
                "certainty_sell_threshold": 0.99,
                "crypto": {
                    "override_enabled": True,
                    "certainty_sell_threshold": 0.95,
                    "near_ceiling_threshold": 0.93,
                    "profit_take_pct": 0.30,
                    "trailing_drop_pct": 0.15,
                    "trailing_min_profit_pct": 0.10,
                    "hold_to_settle_threshold": 0.93,
                    "hold_to_settle_hours": 24.0,
                    "max_hold_hours": 72.0,
                },
                "near_ceiling_threshold": 0.97,
                "trailing_enabled": True,
                "trailing_drop_pct": 0.08,
                "trailing_min_profit_pct": 0.05,
                "hold_to_settle_threshold": 0.95,
                "hold_to_settle_hours": 48.0,
                "profit_take_pct": 0.50,
                "quick_exit_enabled": False,
            },
            "stop_loss": {},
            "sports_live": {},
        }
        mock_get.side_effect = lambda k, *a: _cfg.get(k, {})

        bet = _make_crypto_bet(entry_price=0.40, hours_ago=73.0)
        tracker = _make_tracker({"mkt-crypto-001": 0.42})
        exits = tracker.check_profit_take_exits([bet])
        assert len(exits) == 1
        assert exits[0]["trigger"] == "crypto_max_hold"

    @patch("src.simulation.outcome_tracker.get")
    def test_crypto_max_hold_no_trigger_if_loss(self, mock_get):
        """Crypto 持仓超过 72h 但亏损 → 不触发 crypto_max_hold（交给止损处理）。"""
        _cfg = {
            "profit_taking": {
                "enabled": True,
                "certainty_sell_threshold": 0.99,
                "crypto": {
                    "override_enabled": True,
                    "certainty_sell_threshold": 0.95,
                    "near_ceiling_threshold": 0.93,
                    "profit_take_pct": 0.30,
                    "trailing_drop_pct": 0.15,
                    "trailing_min_profit_pct": 0.10,
                    "hold_to_settle_threshold": 0.93,
                    "hold_to_settle_hours": 24.0,
                    "max_hold_hours": 72.0,
                },
                "near_ceiling_threshold": 0.97,
                "trailing_enabled": True,
                "trailing_drop_pct": 0.08,
                "trailing_min_profit_pct": 0.05,
                "hold_to_settle_threshold": 0.95,
                "hold_to_settle_hours": 48.0,
                "profit_take_pct": 0.50,
                "quick_exit_enabled": False,
            },
            "stop_loss": {},
            "sports_live": {},
        }
        mock_get.side_effect = lambda k, *a: _cfg.get(k, {})

        bet = _make_crypto_bet(entry_price=0.40, hours_ago=73.0)
        tracker = _make_tracker({"mkt-crypto-001": 0.38})  # loss
        exits = tracker.check_profit_take_exits([bet])
        max_hold_exits = [e for e in exits if e.get("trigger") == "crypto_max_hold"]
        assert len(max_hold_exits) == 0

    @patch("src.simulation.outcome_tracker.get")
    def test_crypto_override_disabled_uses_standard(self, mock_get):
        """crypto_override_enabled=false → Crypto 使用标准阈值。
        价格 0.96 不会触发 certainty_sell (标准阈值 0.99)。"""
        _cfg = {
            "profit_taking": {
                "enabled": True,
                "certainty_sell_threshold": 0.99,
                "crypto": {
                    "override_enabled": False,  # 关闭覆盖
                    "certainty_sell_threshold": 0.95,
                    "near_ceiling_threshold": 0.93,
                    "profit_take_pct": 0.30,
                    "trailing_drop_pct": 0.15,
                    "trailing_min_profit_pct": 0.10,
                    "hold_to_settle_threshold": 0.93,
                    "hold_to_settle_hours": 24.0,
                    "max_hold_hours": 72.0,
                },
                "near_ceiling_threshold": 0.97,
                "trailing_enabled": True,
                "trailing_drop_pct": 0.08,
                "trailing_min_profit_pct": 0.05,
                "hold_to_settle_threshold": 0.95,
                "hold_to_settle_hours": 48.0,
                "profit_take_pct": 0.50,
                "quick_exit_enabled": False,
            },
            "stop_loss": {},
            "sports_live": {},
        }
        mock_get.side_effect = lambda k, *a: _cfg.get(k, {})

        bet = _make_crypto_bet(entry_price=0.40)
        tracker = _make_tracker({"mkt-crypto-001": 0.96})
        exits = tracker.check_profit_take_exits([bet])
        certainty_exits = [e for e in exits if e.get("trigger") == "certainty_sell"]
        assert len(certainty_exits) == 0
