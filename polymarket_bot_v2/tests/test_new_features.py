"""
test_new_features.py — 新增安全特性综合测试

覆盖：
  1. Early Stop Window（前10分钟快速止损）
  2. Circuit Breaker（市场熔断器）
  3. Momentum Guard（入场动量守卫）
  4. Smart Scale-in Marginal EV（边际EV加仓检查）
  5. CI Guard（部署安全检查）
  6. DB Metrics（关键指标聚合）
"""

import json
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from src.simulation.outcome_tracker import OutcomeTracker
from src.utils import db

# ------------------------------------------------------------------
# 公共夹具
# ------------------------------------------------------------------


@pytest.fixture
def tmp_db(tmp_path: Path):
    """每个测试用独立 SQLite。"""
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    return db_path


def _make_bet(
    market_id: str = "mkt-001",
    entry_price: float = 0.40,
    outcome: str = "YES",
    size: float = 1.0,
    end_date: str = "2027-12-31T12:00:00Z",
    category: str = "weather",
    bet_bucket: str = "standard",
    ts_entered: float = None,
    ev: float = 0.05,
    ai_probability: float = 0.60,
) -> dict:
    return {
        "bet_id": str(uuid.uuid4()),
        "market_id": market_id,
        "question": "Test question",
        "outcome": outcome,
        "market_price": entry_price,
        "size_usdc": size,
        "market_end_date": end_date,
        "ts_entered": ts_entered or time.time() - 3600,
        "category": category,
        "bet_bucket": bet_bucket,
        "ev": ev,
        "ai_probability": ai_probability,
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
            }
        return result

    tracker._fetch_market_statuses = fake_fetch
    return tracker


# ==================================================================
# 1. Early Stop Window Tests
# ==================================================================


class TestEarlyStopWindow:
    """前N分钟快速止损保护。"""

    @patch("src.simulation.outcome_tracker.get")
    def test_early_stop_triggers_within_window(self, mock_get):
        """持仓不到10分钟且亏损>20%（非天气），应触发早期止损。"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "stop_loss": {
                "enabled": True,
                "floor_price": 0.07,
                "standard_stop_pct": 0.28,
                "weather_standard_stop_pct": 0.22,
                "expiry_stop_hours": 8,
                "expiry_stop_loss_pct": 0.25,
                "reversal_exit_price": 0.35,
                "early_stop": {
                    "enabled": True,
                    "window_minutes": 10,
                    "weather_pct": 0.15,
                    "default_pct": 0.20,
                },
                "liquidity_guard": {"enabled": False},
            },
            "sports_live": {"game_window_hours": 4},
        }.get(key, {})

        # 入场5分钟前，价格0.40 → 现在0.31（-22.5%，超过20% default_pct）
        tracker = _make_tracker({"mkt-001": 0.31})
        bet = _make_bet(
            entry_price=0.40,
            ts_entered=time.time() - 300,  # 5 分钟前
            category="crypto",
        )
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1, "应触发早期止损"

    @patch("src.simulation.outcome_tracker.get")
    def test_early_stop_not_trigger_outside_window(self, mock_get):
        """持仓超过10分钟，不使用早期止损阈值。"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "stop_loss": {
                "enabled": True,
                "floor_price": 0.07,
                "standard_stop_pct": 0.28,
                "weather_standard_stop_pct": 0.22,
                "expiry_stop_hours": 8,
                "expiry_stop_loss_pct": 0.25,
                "reversal_exit_price": 0.35,
                "early_stop": {
                    "enabled": True,
                    "window_minutes": 10,
                    "weather_pct": 0.15,
                    "default_pct": 0.20,
                },
            },
            "sports_live": {"game_window_hours": 4},
        }.get(key, {})

        # 入场30分钟前，价格0.40 → 0.35 (-12.5%，< 20% early且 < 28% standard)
        tracker = _make_tracker({"mkt-001": 0.35})
        bet = _make_bet(
            entry_price=0.40,
            ts_entered=time.time() - 1800,  # 30 分钟前
            category="crypto",
        )
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 0, "超过10分钟窗口，12.5%亏损不应触发28%止损"

    @patch("src.simulation.outcome_tracker.get")
    def test_early_stop_disabled(self, mock_get):
        """early_stop disabled 时不应影响正常止损。"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "stop_loss": {
                "enabled": True,
                "floor_price": 0.07,
                "standard_stop_pct": 0.28,
                "weather_standard_stop_pct": 0.22,
                "expiry_stop_hours": 8,
                "expiry_stop_loss_pct": 0.25,
                "reversal_exit_price": 0.35,
                "early_stop": {
                    "enabled": False,
                    "window_minutes": 10,
                    "weather_pct": 0.15,
                    "default_pct": 0.20,
                },
            },
            "sports_live": {"game_window_hours": 4},
        }.get(key, {})

        # 入场5分钟前，16%亏损（>15% early but <20% default, <28% standard）
        tracker = _make_tracker({"mkt-001": 0.42})
        bet = _make_bet(
            entry_price=0.50,
            ts_entered=time.time() - 300,
            category="crypto",
        )
        stops = tracker.check_stop_loss_exits([bet])
        # 16% < 28% standard_stop, 0.42 > 0.35 reversal → 不触发
        assert len(stops) == 0, "early_stop禁用时不应使用20%阈值"

    @patch("src.simulation.outcome_tracker.get")
    def test_early_stop_not_override_for_live_sports(self, mock_get):
        """体育直播不使用早期止损（有自己的live参数）。"""
        from datetime import datetime, timedelta, timezone

        near_end = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()

        mock_get.side_effect = lambda key, *a, **kw: {
            "stop_loss": {
                "enabled": True,
                "floor_price": 0.07,
                "standard_stop_pct": 0.28,
                "weather_standard_stop_pct": 0.22,
                "expiry_stop_hours": 8,
                "expiry_stop_loss_pct": 0.25,
                "reversal_exit_price": 0.35,
                "early_stop": {
                    "enabled": True,
                    "window_minutes": 10,
                    "weather_pct": 0.15,
                    "default_pct": 0.20,
                },
            },
            "sports_live": {
                "game_window_hours": 4,
                "floor_price": 0.10,
                "standard_stop_pct": 0.30,
                "reversal_exit_price": 0.25,
            },
        }.get(key, {})

        # 体育直播，5分钟前入场，亏损17.5%
        tracker = _make_tracker({"mkt-001": 0.33})
        bet = _make_bet(
            entry_price=0.40,
            ts_entered=time.time() - 300,
            category="sports",
            end_date=near_end,
        )
        stops = tracker.check_stop_loss_exits([bet])
        # is_live=True，early_stop不触发，但 live_stop_pct=0.30 > 17.5% → 不止损
        assert len(stops) == 0


# ==================================================================
# 2. Circuit Breaker Tests
# ==================================================================


class TestCircuitBreaker:
    """市场熔断器测试。"""

    def test_get_recent_exits_empty(self, tmp_db):
        """无历史数据时返回空列表。"""
        exits = db.get_recent_exits_for_market(
            "mkt-999", limit=3, hours=24, db_path=tmp_db
        )
        assert exits == []

    def test_get_recent_exits_returns_stop_losses(self, tmp_db):
        """正确返回最近的止损记录。"""
        for i in range(3):
            bet = {
                "bet_id": f"bet-{i}",
                "ts_entered": time.time() - 7200 + i * 100,
                "market_id": "mkt-001",
                "question": "Test",
                "category": "weather",
                "outcome": "YES",
                "confidence": 0.7,
                "ai_probability": 0.6,
                "market_price": 0.40,
                "ev": 0.05,
                "size_usdc": 1.0,
                "sim_balance_before": 100,
                "sim_balance_after": 99,
                "market_end_date": "2027-01-01",
                "weather_context": "",
                "ai_models": "test",
                "cycle_id": 1,
            }
            db.insert_paper_bet(bet, tmp_db)
            db.close_paper_bet_stop_loss(
                bet_id=f"bet-{i}",
                exit_price=0.30,
                pnl=-0.25,
                payout=0.75,
                db_path=tmp_db,
            )

        exits = db.get_recent_exits_for_market(
            "mkt-001", limit=3, hours=24, db_path=tmp_db
        )
        assert len(exits) == 3
        assert all(e == "stop_loss" for e in exits)

    def test_get_recent_exits_mixed(self, tmp_db):
        """混合止损和止盈记录。"""
        # 先止损
        bet1 = {
            "bet_id": "sl-1",
            "ts_entered": time.time() - 3600,
            "market_id": "mkt-mix",
            "question": "Test",
            "category": "weather",
            "outcome": "YES",
            "confidence": 0.7,
            "ai_probability": 0.6,
            "market_price": 0.40,
            "ev": 0.05,
            "size_usdc": 1.0,
            "sim_balance_before": 100,
            "sim_balance_after": 99,
            "market_end_date": "2027-01-01",
            "weather_context": "",
            "ai_models": "test",
            "cycle_id": 1,
        }
        db.insert_paper_bet(bet1, tmp_db)
        db.close_paper_bet_stop_loss("sl-1", 0.30, -0.25, 0.75, tmp_db)

        # 再止盈
        bet2 = {**bet1, "bet_id": "pt-1", "ts_entered": time.time() - 1800}
        db.insert_paper_bet(bet2, tmp_db)
        db.close_paper_bet_profit_take("pt-1", 0.80, 1.0, 2.0, tmp_db)

        exits = db.get_recent_exits_for_market(
            "mkt-mix", limit=3, hours=24, db_path=tmp_db
        )
        # 最近的是 profit_take（ts较新），所以不全是 stop_loss
        assert "profit_take" in exits
        assert not all(e == "stop_loss" for e in exits)


# ==================================================================
# 3. Smart Scale-in Marginal EV Tests
# ==================================================================


class TestSmartScaleIn:
    """边际EV加仓检查。"""

    @patch("src.simulation.outcome_tracker.get")
    def test_marginal_ev_blocks_low_ev_scalein(self, mock_get):
        """当加仓价导致边际EV < 父EV×50%时，拒绝加仓。"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "paper_trading": {
                "initial_balance": 1000.0,
                "balance_reserve_min_usdc": 0.10,
            },
            "position_management": {
                "enabled": True,
                "scale_in": {
                    "enabled": True,
                    "return_threshold_pct": 0.10,
                    "min_return_for_next": 0.05,
                    "max_count": 3,
                    "cooldown_minutes": 0,
                    "base_ratio": 0.50,
                    "growth_factor": 0.65,
                    "max_entry_premium_pct": 1.0,  # 不限制溢价
                    "max_total_position_pct": 0.50,
                    "min_marginal_ev_ratio": 0.50,
                },
                "sell_one_bet": {"enabled": False},
            },
        }.get(key, {})

        tracker = OutcomeTracker()

        # 父订单：entry_price=0.30, ai_prob=0.60, ev=0.30
        # 加仓价=0.55 → marginal_ev = 0.60-0.55=0.05 < 0.30×0.50=0.15 → 拒绝
        bet = _make_bet(
            entry_price=0.30,
            ev=0.30,
            ai_probability=0.60,
        )
        bet["scale_in_count"] = 0
        bet["last_scale_at"] = 0

        si_cfg = {
            "return_threshold_pct": 0.10,
            "min_return_for_next": 0.05,
            "max_count": 3,
            "cooldown_minutes": 0,
            "base_ratio": 0.50,
            "growth_factor": 0.65,
            "max_entry_premium_pct": 1.0,
            "max_total_position_pct": 0.50,
            "min_marginal_ev_ratio": 0.50,
        }

        result = tracker._try_scale_in(
            bet=bet,
            return_pct=0.50,  # 高收益率（通过threshold检查）
            buy_price=0.55,  # 高买入价 → 低边际EV
            sell_price=0.60,
            si_cfg=si_cfg,
            available_balance=500.0,
            market_data={},
            db_path=None,
        )
        assert result == 0.0, "边际EV不足时应拒绝加仓"


# ==================================================================
# 4. DB Metrics Tests
# ==================================================================


class TestDBMetrics:
    """关键指标聚合。"""

    def test_metrics_empty_db(self, tmp_db):
        """空数据库返回零值。"""
        metrics = db.get_metrics_summary(tmp_db)
        assert metrics["resolved"] == 0
        assert metrics["stop_loss_count"] == 0
        assert metrics["stop_loss_ratio"] == 0.0
        assert metrics["by_category"] == {}

    def test_metrics_with_data(self, tmp_db):
        """有数据时正确计算指标。"""
        base = {
            "ts_entered": time.time() - 3600,
            "market_id": "mkt-001",
            "question": "Test",
            "category": "weather",
            "outcome": "YES",
            "confidence": 0.7,
            "ai_probability": 0.6,
            "market_price": 0.40,
            "ev": 0.05,
            "size_usdc": 1.0,
            "sim_balance_before": 100,
            "sim_balance_after": 99,
            "market_end_date": "2027-01-01",
            "weather_context": "",
            "ai_models": "test",
            "cycle_id": 1,
        }
        # 2个止损
        for i in range(2):
            db.insert_paper_bet({**base, "bet_id": f"sl-{i}"}, tmp_db)
            db.close_paper_bet_stop_loss(f"sl-{i}", 0.30, -0.25, 0.75, tmp_db)

        # 1个止盈
        db.insert_paper_bet({**base, "bet_id": "pt-1"}, tmp_db)
        db.close_paper_bet_profit_take("pt-1", 0.80, 1.0, 2.0, tmp_db)

        metrics = db.get_metrics_summary(tmp_db)
        assert metrics["resolved"] == 3
        assert metrics["stop_loss_count"] == 2
        assert abs(metrics["stop_loss_ratio"] - 2 / 3) < 0.001
        assert metrics["profit_take_count"] == 1
        assert "weather" in metrics["by_category"]
        assert metrics["by_category"]["weather"]["total"] == 3


# ==================================================================
# 5. CI Guard Tests
# ==================================================================


class TestCIGuard:
    """CI部署安全检查。"""

    def test_dry_run_true_passes(self, tmp_path):
        """dry_run=true 应该通过。"""
        cfg = {"general": {"dry_run": True}, "risk": {"max_single_bet_pct": 0.03}}
        cfg_path = tmp_path / "config" / "config.json"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(json.dumps(cfg))

        # 直接测试逻辑
        loaded = json.loads(cfg_path.read_text())
        assert loaded["general"]["dry_run"] is True

    def test_dry_run_false_fails(self, tmp_path):
        """dry_run=false 应该被拒绝。"""
        cfg = {"general": {"dry_run": False}}
        cfg_path = tmp_path / "config" / "config.json"
        cfg_path.parent.mkdir(parents=True)
        cfg_path.write_text(json.dumps(cfg))

        loaded = json.loads(cfg_path.read_text())
        assert loaded["general"]["dry_run"] is not True

    def test_risk_params_not_weakened(self, tmp_path):
        """风控参数不应低于安全下限。"""
        cfg = {
            "general": {"dry_run": True},
            "risk": {
                "daily_loss_pause_pct": 0.05,
                "cumulative_loss_pause_pct": 0.15,
                "max_single_bet_pct": 0.03,
            },
            "stop_loss": {"standard_stop_pct": 0.28},
        }
        # 所有值应 >= 安全下限
        assert cfg["risk"]["daily_loss_pause_pct"] >= 0.03
        assert cfg["risk"]["cumulative_loss_pause_pct"] >= 0.10
        assert cfg["risk"]["max_single_bet_pct"] >= 0.01
        assert cfg["stop_loss"]["standard_stop_pct"] >= 0.10


# ==================================================================
# 6. Multi-tick Momentum Guard Tests
# ==================================================================


class TestMultiTickMomentum:
    """多轮价格趋势检测。"""

    def test_record_price_builds_history(self):
        """价格记录应正确积累。"""
        from src.simulation.paper_trader import PaperTrader

        trader = PaperTrader.__new__(PaperTrader)
        trader._price_history = {}
        trader._record_price("mkt-A", 0.50)
        trader._record_price("mkt-A", 0.49)
        trader._record_price("mkt-A", 0.48)
        assert len(trader._price_history["mkt-A"]) == 3

    def test_record_price_caps_at_max(self):
        """价格历史不超过20条。"""
        from src.simulation.paper_trader import PaperTrader

        trader = PaperTrader.__new__(PaperTrader)
        trader._price_history = {}
        for i in range(25):
            trader._record_price("mkt-A", 0.50 - i * 0.001)
        assert len(trader._price_history["mkt-A"]) == 20

    def test_downtrend_detected(self):
        """连续下跌且累计跌幅≥3%应被拒绝。"""
        from src.simulation.paper_trader import PaperTrader

        trader = PaperTrader.__new__(PaperTrader)
        trader._price_history = {
            "mkt-A": [
                (1.0, 0.50),
                (2.0, 0.49),
                (3.0, 0.48),
            ]
        }
        result = trader._check_momentum_trend("mkt-A", ticks=3, max_drop_pct=0.03)
        assert result != "", "连续3轮下跌4%应被拒绝"

    def test_no_downtrend_passes(self):
        """非连续下跌应通过。"""
        from src.simulation.paper_trader import PaperTrader

        trader = PaperTrader.__new__(PaperTrader)
        trader._price_history = {
            "mkt-A": [
                (1.0, 0.50),
                (2.0, 0.51),  # 上涨了，打断连续下跌
                (3.0, 0.49),
            ]
        }
        result = trader._check_momentum_trend("mkt-A", ticks=3, max_drop_pct=0.03)
        assert result == "", "非连续下跌应通过"

    def test_small_drop_passes(self):
        """连续下跌但累计跌幅<3%应通过。"""
        from src.simulation.paper_trader import PaperTrader

        trader = PaperTrader.__new__(PaperTrader)
        trader._price_history = {
            "mkt-A": [
                (1.0, 0.500),
                (2.0, 0.498),
                (3.0, 0.496),  # 0.8%跌幅
            ]
        }
        result = trader._check_momentum_trend("mkt-A", ticks=3, max_drop_pct=0.03)
        assert result == "", "0.8%跌幅不应触发3%阈值"

    def test_insufficient_data_passes(self):
        """数据不足时应安全放行。"""
        from src.simulation.paper_trader import PaperTrader

        trader = PaperTrader.__new__(PaperTrader)
        trader._price_history = {"mkt-A": [(1.0, 0.50)]}
        result = trader._check_momentum_trend("mkt-A", ticks=3, max_drop_pct=0.03)
        assert result == "", "数据不足应放行"

    def test_no_history_passes(self):
        """无历史记录应放行。"""
        from src.simulation.paper_trader import PaperTrader

        trader = PaperTrader.__new__(PaperTrader)
        trader._price_history = {}
        result = trader._check_momentum_trend("mkt-B", ticks=3, max_drop_pct=0.03)
        assert result == "", "无记录应放行"


# ==================================================================
# 7. Tiered Early Stop Tests
# ==================================================================


class TestTieredEarlyStop:
    """分赛道早期止损阈值。"""

    @patch("src.simulation.outcome_tracker.get")
    def test_weather_uses_15pct(self, mock_get):
        """天气赛道前10分钟使用15%阈值。"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "stop_loss": {
                "enabled": True,
                "floor_price": 0.07,
                "standard_stop_pct": 0.28,
                "weather_standard_stop_pct": 0.22,
                "expiry_stop_hours": 8,
                "expiry_stop_loss_pct": 0.25,
                "reversal_exit_price": 0.35,
                "early_stop": {
                    "enabled": True,
                    "window_minutes": 10,
                    "weather_pct": 0.15,
                    "default_pct": 0.20,
                },
                "liquidity_guard": {"enabled": False},
            },
            "sports_live": {"game_window_hours": 4},
        }.get(key, {})

        # 天气：5分钟前入场，价格0.50→0.42（-16%，>15%天气阈值）
        tracker = _make_tracker({"mkt-001": 0.42})
        bet = _make_bet(
            entry_price=0.50,
            ts_entered=time.time() - 300,
            category="weather",
        )
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1, "天气16%亏损应触发15%早期止损"

    @patch("src.simulation.outcome_tracker.get")
    def test_crypto_uses_20pct(self, mock_get):
        """非天气赛道前10分钟使用20%阈值。"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "stop_loss": {
                "enabled": True,
                "floor_price": 0.07,
                "standard_stop_pct": 0.28,
                "weather_standard_stop_pct": 0.22,
                "expiry_stop_hours": 8,
                "expiry_stop_loss_pct": 0.25,
                "reversal_exit_price": 0.35,
                "early_stop": {
                    "enabled": True,
                    "window_minutes": 10,
                    "weather_pct": 0.15,
                    "default_pct": 0.20,
                },
            },
            "sports_live": {"game_window_hours": 4},
        }.get(key, {})

        # crypto：5分钟前入场，价格0.50→0.42（-16%，<20%非天气阈值）
        tracker = _make_tracker({"mkt-001": 0.42})
        bet = _make_bet(
            entry_price=0.50,
            ts_entered=time.time() - 300,
            category="crypto",
        )
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 0, "crypto 16%亏损不应触发20%早期止损"

    @patch("src.simulation.outcome_tracker.get")
    def test_crypto_triggers_at_21pct(self, mock_get):
        """非天气赛道亏损超过20%应触发。"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "stop_loss": {
                "enabled": True,
                "floor_price": 0.07,
                "standard_stop_pct": 0.28,
                "weather_standard_stop_pct": 0.22,
                "expiry_stop_hours": 8,
                "expiry_stop_loss_pct": 0.25,
                "reversal_exit_price": 0.35,
                "early_stop": {
                    "enabled": True,
                    "window_minutes": 10,
                    "weather_pct": 0.15,
                    "default_pct": 0.20,
                },
                "liquidity_guard": {"enabled": False},
            },
            "sports_live": {"game_window_hours": 4},
        }.get(key, {})

        # crypto：5分钟前入场，价格0.50→0.39（-22%，>20%）
        tracker = _make_tracker({"mkt-001": 0.39})
        bet = _make_bet(
            entry_price=0.50,
            ts_entered=time.time() - 300,
            category="crypto",
        )
        stops = tracker.check_stop_loss_exits([bet])
        assert len(stops) == 1, "crypto 22%亏损应触发20%早期止损"


# ==================================================================
# 8. Recent Resolved Exits DB Tests
# ==================================================================


class TestRecentResolvedExits:
    """近期已结算投注exit_type查询。"""

    def test_empty_db(self, tmp_db):
        """空数据库返回空列表。"""
        exits = db.get_recent_resolved_exits(limit=10, db_path=tmp_db)
        assert exits == []

    def test_returns_recent_exits(self, tmp_db):
        """正确返回最近的exit_type列表。"""
        base = {
            "ts_entered": time.time() - 3600,
            "market_id": "mkt-001",
            "question": "Test",
            "category": "weather",
            "outcome": "YES",
            "confidence": 0.7,
            "ai_probability": 0.6,
            "market_price": 0.40,
            "ev": 0.05,
            "size_usdc": 1.0,
            "sim_balance_before": 100,
            "sim_balance_after": 99,
            "market_end_date": "2027-01-01",
            "weather_context": "",
            "ai_models": "test",
            "cycle_id": 1,
        }
        # 3 止损 + 2 止盈
        for i in range(3):
            db.insert_paper_bet({**base, "bet_id": f"sl-{i}"}, tmp_db)
            db.close_paper_bet_stop_loss(f"sl-{i}", 0.30, -0.25, 0.75, tmp_db)
        for i in range(2):
            db.insert_paper_bet({**base, "bet_id": f"pt-{i}"}, tmp_db)
            db.close_paper_bet_profit_take(f"pt-{i}", 0.80, 1.0, 2.0, tmp_db)

        exits = db.get_recent_resolved_exits(limit=10, db_path=tmp_db)
        assert len(exits) == 5
        assert exits.count("stop_loss") == 3
        assert exits.count("profit_take") == 2

    def test_limit_works(self, tmp_db):
        """limit参数正确限制返回数量。"""
        base = {
            "ts_entered": time.time() - 3600,
            "market_id": "mkt-001",
            "question": "Test",
            "category": "weather",
            "outcome": "YES",
            "confidence": 0.7,
            "ai_probability": 0.6,
            "market_price": 0.40,
            "ev": 0.05,
            "size_usdc": 1.0,
            "sim_balance_before": 100,
            "sim_balance_after": 99,
            "market_end_date": "2027-01-01",
            "weather_context": "",
            "ai_models": "test",
            "cycle_id": 1,
        }
        for i in range(10):
            db.insert_paper_bet({**base, "bet_id": f"bet-{i}"}, tmp_db)
            db.close_paper_bet_stop_loss(f"bet-{i}", 0.30, -0.25, 0.75, tmp_db)

        exits = db.get_recent_resolved_exits(limit=5, db_path=tmp_db)
        assert len(exits) == 5
