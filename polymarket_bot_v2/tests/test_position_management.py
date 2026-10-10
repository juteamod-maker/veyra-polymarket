"""
test_position_management.py — 持仓管理 v2 单元测试

验证（遵循 Polymarket 原子订单规则）：
  1. 加仓条件正确触发 / 不触发
  2. 加仓创建新的独立 paper_bet 记录（bet_type='scale_in'）
  3. 卖出整笔：同市场 ≥2 笔时可卖出完整订单
  4. 卖出整笔：同市场仅 1 笔时不可减仓
  5. 冷却期 / 总仓位上限 / 余额保护
  6. 父订单追踪字段正确更新
  7. 买入价 / 卖出价正确提取
"""

import json
import time
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from src.simulation.outcome_tracker import OutcomeTracker
from src.utils import db

# ------------------------------------------------------------------
# 测试夹具
# ------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path):
    """每个测试用独立的 SQLite，互不干扰。"""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    return db_path


def _make_bet(
    market_id: str = "mkt-001",
    entry_price: float = 0.40,
    outcome: str = "YES",
    size: float = 5.0,
    end_date: str = "2027-12-31T12:00:00Z",
    scale_in_count: int = 0,
    last_scale_at: float = 0.0,
    bet_type: str = "entry",
    parent_bet_id: str = None,
    position_history: str = "[]",
) -> dict:
    """生成一条标准模拟投注记录。"""
    return {
        "bet_id": str(uuid.uuid4()),
        "market_id": market_id,
        "question": "Will the highest temperature in Seoul be 15°C?",
        "category": "weather",
        "outcome": outcome,
        "market_price": entry_price,
        "size_usdc": size,
        "market_end_date": end_date,
        "ts_entered": time.time() - 3600,
        "confidence": 0.75,
        "ai_probability": 0.60,
        "ev": 0.05,
        "sim_balance_before": 1000.0,
        "sim_balance_after": 995.0,
        "weather_context": "",
        "ai_models": "gpt-4o",
        "cycle_id": 1,
        "scale_in_count": scale_in_count,
        "last_scale_at": last_scale_at,
        "bet_type": bet_type,
        "parent_bet_id": parent_bet_id,
        "position_history": position_history,
    }


def _make_tracker(current_prices: dict) -> OutcomeTracker:
    """创建 OutcomeTracker，mock Gamma API 返回固定价格。"""
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


def _insert_bet(bet: dict, db_path: Path) -> None:
    """插入投注到测试 DB。"""
    db.insert_paper_bet(bet, db_path)
    # 更新 position management 字段
    with db.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE paper_bets
            SET scale_in_count=?, last_scale_at=?, position_history=?,
                bet_type=?, parent_bet_id=?
            WHERE bet_id=?
            """,
            (
                bet.get("scale_in_count", 0),
                bet.get("last_scale_at"),
                bet.get("position_history", "[]"),
                bet.get("bet_type", "entry"),
                bet.get("parent_bet_id"),
                bet["bet_id"],
            ),
        )


# ------------------------------------------------------------------
# 加仓（Scale-In）测试
# ------------------------------------------------------------------


PM_CFG_ENABLED = {
    "enabled": True,
    "scale_in": {
        "enabled": True,
        "return_threshold_pct": 0.10,
        "min_return_for_next": 0.05,
        "max_count": 5,
        "cooldown_minutes": 15,
        "base_ratio": 0.60,
        "growth_factor": 0.80,
        "max_total_position_pct": 0.15,
        "max_entry_premium_pct": 0.15,
    },
    "sell_one_bet": {
        "enabled": True,
        "sell_winner_return_pct": 0.50,
        "sell_loser_loss_pct": 0.20,
    },
}


def _cfg_side_effect(key):
    if key == "position_management":
        return PM_CFG_ENABLED
    if key == "paper_trading":
        return {"balance_reserve_min_usdc": 0.10, "initial_balance": 1000.0}
    return None


# ------------------------------------------------------------------
# 加仓（Scale-In）测试 — 创建新独立订单
# ------------------------------------------------------------------


class TestScaleIn:
    """加仓触发条件：创建新 paper_bet 记录，而非修改现有记录。"""

    @patch("src.simulation.outcome_tracker.get")
    def test_scale_in_creates_new_bet(self, mock_get, tmp_db):
        """盈利 ≥ 10% → 加仓创建新的独立订单。"""
        mock_get.side_effect = _cfg_side_effect

        bet = _make_bet(entry_price=0.43, size=5.0)
        _insert_bet(bet, tmp_db)

        # 当前价 0.48 → 盈利 11.6% > 10% threshold; bestAsk=0.49 < 0.43*1.15=0.4945
        tracker = _make_tracker({"mkt-001": 0.48})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["scale_ins"] == 1

        # 验证：DB 中现在有 2 条记录（原始 + 加仓）
        all_bets = db.get_open_paper_bets(tmp_db)
        assert len(all_bets) == 2

        # 原始订单不变（size 不变）
        parent = [b for b in all_bets if (b.get("bet_type") or "entry") == "entry"][0]
        assert parent["size_usdc"] == 5.0
        assert parent["scale_in_count"] == 1

        # 新订单是 scale_in 类型
        child = [b for b in all_bets if b.get("bet_type") == "scale_in"][0]
        assert child["parent_bet_id"] == bet["bet_id"]
        assert child["market_id"] == "mkt-001"
        assert child["size_usdc"] > 0
        # 买入价应是 bestAsk（0.49）
        assert child["market_price"] == pytest.approx(0.49, abs=0.01)

    @patch("src.simulation.outcome_tracker.get")
    def test_no_scale_in_below_threshold(self, mock_get, tmp_db):
        """盈利 < 10% → 不加仓。"""
        mock_get.side_effect = _cfg_side_effect

        bet = _make_bet(entry_price=0.40, size=5.0)
        _insert_bet(bet, tmp_db)

        # 当前价 0.42 → 盈利 5% < 10%
        tracker = _make_tracker({"mkt-001": 0.42})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["scale_ins"] == 0
        assert len(db.get_open_paper_bets(tmp_db)) == 1

    @patch("src.simulation.outcome_tracker.get")
    def test_scale_in_max_count_respected(self, mock_get, tmp_db):
        """已加仓 5 次 → 第 6 次不再加仓（max_count=5）。"""
        mock_get.side_effect = _cfg_side_effect

        bet = _make_bet(
            entry_price=0.40,
            size=5.0,
            scale_in_count=5,
        )
        _insert_bet(bet, tmp_db)

        tracker = _make_tracker({"mkt-001": 0.55})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["scale_ins"] == 0

    @patch("src.simulation.outcome_tracker.get")
    def test_scale_in_cooldown(self, mock_get, tmp_db):
        """冷却期未过（15min）→ 不加仓。"""
        mock_get.side_effect = _cfg_side_effect

        bet = _make_bet(
            entry_price=0.40,
            size=5.0,
            scale_in_count=1,
            last_scale_at=time.time() - 600,  # 10 min ago (< 15 min cooldown)
        )
        _insert_bet(bet, tmp_db)

        tracker = _make_tracker({"mkt-001": 0.55})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["scale_ins"] == 0

    @patch("src.simulation.outcome_tracker.get")
    def test_scale_in_respects_max_total_position(self, mock_get, tmp_db):
        """同市场总仓位已达上限 → 不加仓。"""
        mock_get.side_effect = _cfg_side_effect

        # balance=900, max_total_position_pct=0.15 → max_total=150
        # 但 size=150 已满
        bet = _make_bet(
            entry_price=0.40,
            size=150.0,
        )
        _insert_bet(bet, tmp_db)

        tracker = _make_tracker({"mkt-001": 0.55})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["scale_ins"] == 0

    @patch("src.simulation.outcome_tracker.get")
    def test_scale_in_insufficient_balance(self, mock_get, tmp_db):
        """余额不足 → 不加仓。"""
        mock_get.side_effect = _cfg_side_effect

        bet = _make_bet(entry_price=0.40, size=5.0)
        _insert_bet(bet, tmp_db)

        tracker = _make_tracker({"mkt-001": 0.48})
        open_bets = db.get_open_paper_bets(tmp_db)

        # balance=100, reserve=100 (10% of 1000) → no room
        result = tracker.check_position_scaling(open_bets, 100.0, tmp_db)
        assert result["scale_ins"] == 0

    @patch("src.simulation.outcome_tracker.get")
    def test_scale_in_progressive_threshold(self, mock_get, tmp_db):
        """第 2 次加仓需要 10%+5%=15% 盈利。"""
        mock_get.side_effect = _cfg_side_effect

        bet = _make_bet(
            entry_price=0.40,
            size=5.0,
            scale_in_count=1,
            last_scale_at=time.time() - 3600,  # cooldown passed
        )
        _insert_bet(bet, tmp_db)

        # 当前价 0.45 → 盈利 = (0.45-0.40)/0.40 = 12.5%
        # 第2次需要 10% + 1×5% = 15% → 不足
        tracker = _make_tracker({"mkt-001": 0.45})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["scale_ins"] == 0

    @patch("src.simulation.outcome_tracker.get")
    def test_only_entry_bets_trigger_scale_in(self, mock_get, tmp_db):
        """只有 entry 类型的父订单才能触发加仓，scale_in 子订单不触发。"""
        mock_get.side_effect = _cfg_side_effect

        parent = _make_bet(entry_price=0.49, size=5.0)
        _insert_bet(parent, tmp_db)

        child = _make_bet(
            entry_price=0.50,
            size=3.0,
            bet_type="scale_in",
            parent_bet_id=parent["bet_id"],
        )
        _insert_bet(child, tmp_db)

        # 当前价 0.55 → 两笔都盈利，但只有 entry 类型触发加仓
        # bestAsk=0.56, 0.49*1.15=0.5635 > 0.56 → premium check passes
        tracker = _make_tracker({"mkt-001": 0.55})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["scale_ins"] == 1  # 只有 parent 触发

        # DB 中应有 3 条记录
        all_bets = db.get_open_paper_bets(tmp_db)
        assert len(all_bets) == 3

    @patch("src.simulation.outcome_tracker.get")
    def test_scale_in_blocked_by_entry_premium(self, mock_get, tmp_db):
        """加仓价超过初始入场价+15% → 跳过加仓（防止高位追涨）。"""
        mock_get.side_effect = _cfg_side_effect

        # entry=0.40, current=0.55, bestAsk=0.56
        # 0.56 > 0.40*1.15=0.46 → premium check blocks
        bet = _make_bet(entry_price=0.40, size=5.0)
        _insert_bet(bet, tmp_db)

        tracker = _make_tracker({"mkt-001": 0.55})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["scale_ins"] == 0  # blocked by premium


# ------------------------------------------------------------------
# 卖出整笔（Sell-One-Bet）测试
# ------------------------------------------------------------------


def _cfg_sell_only_side_effect(key):
    """卖出测试专用：禁用加仓，只测卖出逻辑。"""
    if key == "position_management":
        return {
            "enabled": True,
            "scale_in": {"enabled": False},
            "sell_one_bet": PM_CFG_ENABLED["sell_one_bet"],
        }
    if key == "paper_trading":
        return {"balance_reserve_min_usdc": 0.10, "initial_balance": 1000.0}
    return None


class TestSellOneBet:
    """卖出整笔订单：遵循 Polymarket 原子订单规则。"""

    @patch("src.simulation.outcome_tracker.get")
    def test_cannot_sell_with_single_bet(self, mock_get, tmp_db):
        """只有 1 笔订单时不可减仓。"""
        mock_get.side_effect = _cfg_sell_only_side_effect

        bet = _make_bet(entry_price=0.40, size=5.0)
        _insert_bet(bet, tmp_db)

        # 即使盈利 100%，也不能卖出（因为只有 1 笔）
        tracker = _make_tracker({"mkt-001": 0.80})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["sold_bets"] == 0
        assert len(db.get_open_paper_bets(tmp_db)) == 1

    @patch("src.simulation.outcome_tracker.get")
    def test_sell_winner_with_two_bets(self, mock_get, tmp_db):
        """同市场 2 笔 + 最佳盈利 ≥ 50% → 卖出盈利最高的。"""
        mock_get.side_effect = _cfg_sell_only_side_effect

        parent = _make_bet(entry_price=0.40, size=5.0)
        _insert_bet(parent, tmp_db)

        child = _make_bet(
            entry_price=0.45,
            size=3.0,
            bet_type="scale_in",
            parent_bet_id=parent["bet_id"],
        )
        _insert_bet(child, tmp_db)

        # 当前价 0.65 → parent: (0.65-0.40)/0.40=62.5% > 50%
        #                child:  (0.65-0.45)/0.45=44.4% < 50%
        tracker = _make_tracker({"mkt-001": 0.65})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["sold_bets"] == 1

        # parent (盈利最高) 被卖出
        remaining = db.get_open_paper_bets(tmp_db)
        assert len(remaining) == 1
        assert remaining[0]["bet_type"] == "scale_in"

    @patch("src.simulation.outcome_tracker.get")
    def test_sell_loser_with_two_bets(self, mock_get, tmp_db):
        """同市场 2 笔 + 最差亏损 ≥ 20% → 卖出亏损最重的。"""
        mock_get.side_effect = _cfg_sell_only_side_effect

        parent = _make_bet(entry_price=0.40, size=5.0)
        _insert_bet(parent, tmp_db)

        child = _make_bet(
            entry_price=0.50,
            size=3.0,
            bet_type="scale_in",
            parent_bet_id=parent["bet_id"],
        )
        _insert_bet(child, tmp_db)

        # 当前价 0.38 → parent: (0.38-0.40)/0.40 = -5% (not bad enough)
        #                child:  (0.38-0.50)/0.50 = -24% > 20% loss
        tracker = _make_tracker({"mkt-001": 0.38})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["sold_bets"] == 1

        # child (亏损最重) 被卖出
        remaining = db.get_open_paper_bets(tmp_db)
        assert len(remaining) == 1
        assert remaining[0]["bet_id"] == parent["bet_id"]

    @patch("src.simulation.outcome_tracker.get")
    def test_no_sell_when_neither_threshold_met(self, mock_get, tmp_db):
        """同市场 2 笔但盈利/亏损都未达阈值 → 不卖。"""
        mock_get.side_effect = _cfg_sell_only_side_effect

        parent = _make_bet(entry_price=0.40, size=5.0)
        _insert_bet(parent, tmp_db)

        child = _make_bet(
            entry_price=0.42,
            size=3.0,
            bet_type="scale_in",
            parent_bet_id=parent["bet_id"],
        )
        _insert_bet(child, tmp_db)

        # 当前价 0.45 → parent: +12.5%, child: +7.1% — 都不够卖
        tracker = _make_tracker({"mkt-001": 0.45})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["sold_bets"] == 0
        assert len(db.get_open_paper_bets(tmp_db)) == 2


# ------------------------------------------------------------------
# 功能开关测试
# ------------------------------------------------------------------


class TestPositionManagementToggle:
    """配置开关测试。"""

    @patch("src.simulation.outcome_tracker.get")
    def test_disabled_returns_zeros(self, mock_get, tmp_db):
        """position_management.enabled=false → 不执行任何操作。"""
        mock_get.return_value = {"enabled": False}

        bet = _make_bet(entry_price=0.40, size=5.0)
        _insert_bet(bet, tmp_db)

        tracker = _make_tracker({"mkt-001": 0.60})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["scale_ins"] == 0
        assert result["sold_bets"] == 0

    @patch("src.simulation.outcome_tracker.get")
    def test_scale_in_disabled_only(self, mock_get, tmp_db):
        """scale_in.enabled=false → 不加仓但可以卖出。"""
        cfg = {
            "enabled": True,
            "scale_in": {"enabled": False},
            "sell_one_bet": PM_CFG_ENABLED["sell_one_bet"],
        }

        def side_effect(key):
            if key == "position_management":
                return cfg
            if key == "paper_trading":
                return {"balance_reserve_min_usdc": 0.10, "initial_balance": 1000.0}
            return None

        mock_get.side_effect = side_effect

        parent = _make_bet(entry_price=0.40, size=5.0)
        _insert_bet(parent, tmp_db)
        child = _make_bet(
            entry_price=0.45,
            size=3.0,
            bet_type="scale_in",
            parent_bet_id=parent["bet_id"],
        )
        _insert_bet(child, tmp_db)

        # 高盈利 → 应该卖出但不加仓
        tracker = _make_tracker({"mkt-001": 0.70})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["scale_ins"] == 0
        assert result["sold_bets"] == 1

    @patch("src.simulation.outcome_tracker.get")
    def test_sell_one_bet_disabled(self, mock_get, tmp_db):
        """sell_one_bet.enabled=false → 不卖出但可以加仓。"""
        cfg = {
            "enabled": True,
            "scale_in": PM_CFG_ENABLED["scale_in"],
            "sell_one_bet": {"enabled": False},
        }

        def side_effect(key):
            if key == "position_management":
                return cfg
            if key == "paper_trading":
                return {"balance_reserve_min_usdc": 0.10, "initial_balance": 1000.0}
            return None

        mock_get.side_effect = side_effect

        bet = _make_bet(entry_price=0.43, size=5.0)
        _insert_bet(bet, tmp_db)

        tracker = _make_tracker({"mkt-001": 0.48})
        open_bets = db.get_open_paper_bets(tmp_db)

        result = tracker.check_position_scaling(open_bets, 900.0, tmp_db)
        assert result["scale_ins"] == 1
        assert result["sold_bets"] == 0


# ------------------------------------------------------------------
# Position History 追踪测试
# ------------------------------------------------------------------


class TestPositionHistory:
    """​position_history JSON 字段正确记录加仓操作历史。"""

    @patch("src.simulation.outcome_tracker.get")
    def test_scale_in_history_recorded(self, mock_get, tmp_db):
        """加仓后 position_history 包含 scale_in 记录和 child_bet_id。"""
        mock_get.side_effect = _cfg_side_effect

        bet = _make_bet(entry_price=0.43, size=5.0)
        _insert_bet(bet, tmp_db)

        tracker = _make_tracker({"mkt-001": 0.48})
        open_bets = db.get_open_paper_bets(tmp_db)
        tracker.check_position_scaling(open_bets, 900.0, tmp_db)

        all_bets = db.get_open_paper_bets(tmp_db)
        parent = [b for b in all_bets if (b.get("bet_type") or "entry") == "entry"][0]
        history = json.loads(parent["position_history"])
        assert len(history) == 1
        assert history[0]["action"] == "scale_in"
        assert "child_bet_id" in history[0]
        assert history[0]["size"] > 0


# ------------------------------------------------------------------
# 买入价提取测试
# ------------------------------------------------------------------


class TestBuyPrice:
    """_extract_buy_price 方法测试。"""

    def test_buy_price_yes(self):
        """YES 买入价 = bestAsk。"""
        mdata = {"bestBid": 0.48, "bestAsk": 0.50}
        price = OutcomeTracker._extract_buy_price(mdata, "YES")
        assert price == 0.50

    def test_buy_price_no(self):
        """NO 买入价 = 1 - bestBid。"""
        mdata = {"bestBid": 0.48, "bestAsk": 0.50}
        price = OutcomeTracker._extract_buy_price(mdata, "NO")
        assert price == pytest.approx(0.52, abs=0.01)

    def test_sell_price_yes(self):
        """YES 卖出价 = bestBid。"""
        mdata = {"bestBid": 0.48, "bestAsk": 0.50}
        price = OutcomeTracker._extract_sell_price(mdata, "YES")
        assert price == 0.48

    def test_sell_price_no(self):
        """NO 卖出价 = 1 - bestAsk。"""
        mdata = {"bestBid": 0.48, "bestAsk": 0.50}
        price = OutcomeTracker._extract_sell_price(mdata, "NO")
        assert price == pytest.approx(0.50, abs=0.01)


# ------------------------------------------------------------------
# DB 新函数测试
# ------------------------------------------------------------------


class TestDBFunctions:
    """测试 db.py 新增/修改的函数。"""

    def test_count_open_entry_bets(self, tmp_db):
        """count_open_entry_bets_for_market 只计入 entry 类型。"""
        parent = _make_bet(market_id="mkt-001", entry_price=0.40)
        _insert_bet(parent, tmp_db)

        child = _make_bet(
            market_id="mkt-001",
            entry_price=0.45,
            bet_type="scale_in",
            parent_bet_id=parent["bet_id"],
        )
        _insert_bet(child, tmp_db)

        entry_count = db.count_open_entry_bets_for_market("mkt-001", tmp_db)
        total_count = db.count_open_bets_for_market("mkt-001", tmp_db)

        assert entry_count == 1
        assert total_count == 2

    def test_get_open_bets_for_market(self, tmp_db):
        """get_open_bets_for_market 返回指定市场所有活跃订单。"""
        bet1 = _make_bet(market_id="mkt-001", entry_price=0.40)
        _insert_bet(bet1, tmp_db)

        bet2 = _make_bet(market_id="mkt-002", entry_price=0.50)
        _insert_bet(bet2, tmp_db)

        bets = db.get_open_bets_for_market("mkt-001", tmp_db)
        assert len(bets) == 1
        assert bets[0]["market_id"] == "mkt-001"

    def test_insert_paper_bet_with_bet_type(self, tmp_db):
        """insert_paper_bet 正确写入 bet_type 和 parent_bet_id。"""
        parent = _make_bet(entry_price=0.40)
        db.insert_paper_bet(parent, tmp_db)

        child_data = _make_bet(
            entry_price=0.45,
            bet_type="scale_in",
            parent_bet_id=parent["bet_id"],
        )
        db.insert_paper_bet(child_data, tmp_db)

        bets = db.get_open_paper_bets(tmp_db)
        assert len(bets) == 2

        child = [b for b in bets if b["bet_id"] == child_data["bet_id"]][0]
        assert child["bet_type"] == "scale_in"
        assert child["parent_bet_id"] == parent["bet_id"]


# ------------------------------------------------------------------
# 止盈止损使用各自入场价测试（无 avg_entry_price）
# ------------------------------------------------------------------


class TestIndependentEntryPriceInExits:
    """每笔订单独立止盈/止损，使用各自的 market_price。"""

    def test_profit_take_uses_own_entry_price(self, tmp_db):
        """止盈 return_pct 基于该笔订单自己的 market_price 计算。"""
        bet = _make_bet(entry_price=0.40, size=5.0)
        _insert_bet(bet, tmp_db)

        # 当前价 0.97 → 天花板止盈
        tracker = _make_tracker({"mkt-001": 0.97})
        open_bets = db.get_open_paper_bets(tmp_db)

        exits = tracker.check_profit_take_exits(open_bets)
        assert len(exits) == 1
        expected_return = (0.97 - 0.40) / 0.40
        assert abs(exits[0]["return_pct"] - expected_return) < 0.01

    def test_stop_loss_uses_own_entry_price(self, tmp_db):
        """止损 return_pct 基于该笔订单自己的 market_price 计算。"""
        bet = _make_bet(entry_price=0.40, size=5.0)
        _insert_bet(bet, tmp_db)

        # 当前价 0.02 → floor 止损（floor_price=0.03）
        tracker = _make_tracker({"mkt-001": 0.02})
        open_bets = db.get_open_paper_bets(tmp_db)

        stops = tracker.check_stop_loss_exits(open_bets)
        assert len(stops) == 1
        expected_return = (0.02 - 0.40) / 0.40
        assert abs(stops[0]["return_pct"] - expected_return) < 0.01

    def test_scale_in_bet_independent_tp(self, tmp_db):
        """加仓订单的止盈基于自己的入场价，不受父订单影响。"""
        parent = _make_bet(entry_price=0.40, size=5.0)
        _insert_bet(parent, tmp_db)

        child = _make_bet(
            entry_price=0.50,
            size=3.0,
            bet_type="scale_in",
            parent_bet_id=parent["bet_id"],
        )
        _insert_bet(child, tmp_db)

        # 当前价 0.99 → 两笔都触发天花板止盈
        tracker = _make_tracker({"mkt-001": 0.99})
        open_bets = db.get_open_paper_bets(tmp_db)

        exits = tracker.check_profit_take_exits(open_bets)
        assert len(exits) == 2

        # 各自 return_pct 不同
        parent_exit = [e for e in exits if e["bet_id"] == parent["bet_id"]][0]
        child_exit = [e for e in exits if e["bet_id"] == child["bet_id"]][0]

        assert abs(parent_exit["return_pct"] - (0.99 - 0.40) / 0.40) < 0.01
        assert abs(child_exit["return_pct"] - (0.99 - 0.50) / 0.50) < 0.01
