"""
test_risk_realized_pnl.py — Phase 39 修复验证

测试目标：
  风控每日亏损应仅统计「已结算投注亏损」，而非原始余额下降
  （买入期权/下单部署资金 ≠ 亏损，资金仍在开仓仓位中）

根因：update_balance() 将"开仓时 USDC 减少"计入 _day_pnl，
     导致 63 USDC 余额 + 37 USDC 仓位 = 错报 58.2% 日亏损并暂停系统。

修复：
  RiskMonitor 新增 set_day_realized_pnl() / set_cum_realized_loss()
  → check() 优先使用 DB 已结算 PnL，None 时回退余额追踪（兼容测试）
"""

import sqlite3
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from src.monitor.risk_monitor import RiskMonitor
from src.utils import db as db_mod

# ───────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────


@pytest.fixture()
def tmp_db(tmp_path):
    """创建临时 SQLite 数据库并初始化 schema。"""
    db_path = tmp_path / "test.db"
    db_mod.init_db(db_path)
    return db_path


def _smart_cfg(k):
    """宽松 smart_pause 设置，用于不触发小余额逻辑的测试。"""
    cfg = {
        "risk.smart_pause": {
            "enabled": True,
            "min_absolute_daily_loss_usdc": 5.0,
            "min_absolute_cumulative_loss_usdc": 3.0,
            "small_balance_threshold_usdc": 20.0,
            "small_balance_daily_pause_pct": 0.25,
            "small_balance_cumulative_pause_pct": 0.50,
        },
        "risk.daily_loss_pause_pct": 0.50,  # 50% 日亏门槛
        "risk.cumulative_loss_pause_pct": 0.50,
        "risk.withdraw_trigger_pct": 0.50,
        "risk.category_pause_on_loss_streak": 3,
    }
    return cfg.get(k)


# ───────────────────────────────────────────────
# 核心修复测试
# ───────────────────────────────────────────────


class TestRealizedPnlFix:
    """verify deployed capital is NOT treated as daily loss"""

    @patch("src.monitor.risk_monitor.get")
    def test_deployed_capital_not_counted_as_daily_loss(self, mock_get):
        """
        场景：余额从 100 降到 63（37 USDC 部署为开仓），无任何结算亏损。
        期望：risk monitor 不触发暂停（日亏损 = 0，因无结算记录）。
        """
        mock_get.side_effect = _smart_cfg
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(100.0)
        m.update_balance(63.0)  # -37 USDC: 部署至开仓仓位

        # 注入：今日结算亏损 = 0（没有仓位结算）
        m.set_day_realized_pnl(0.0)
        m.set_cum_realized_loss(0.0)

        alerts = m.check([])
        assert not m.globally_paused, "开仓部署资金不应被计为当日亏损——仓位还未结算！"
        assert len(alerts) == 0

    @patch("src.monitor.risk_monitor.get")
    def test_settled_loss_triggers_pause(self, mock_get):
        """
        场景：今日结算亏损 36 USDC / 当前余额 63 USDC = 57.1% > 50% 门槛。
        期望：risk monitor 触发暂停（结算数据明确为亏损）。
        """
        mock_get.side_effect = _smart_cfg
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(63.0)

        # 注入：真实结算亏损 36 USDC（超过 50% 门槛）
        m.set_day_realized_pnl(-36.0)

        alerts = m.check([])
        assert m.globally_paused, "真实结算亏损 >50% 应触发暂停"
        assert m.pause_reason == "daily"
        # 告警信息应包含 DB结算 标记
        assert any("DB结算" in a for a in alerts)

    @patch("src.monitor.risk_monitor.get")
    def test_small_settled_loss_no_pause_below_floor(self, mock_get):
        """
        场景：结算亏损仅 3 USDC < min_absolute_daily_loss 5 USDC。
        期望：不触发暂停（绝对金额地板保护）。
        """
        mock_get.side_effect = _smart_cfg
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(97.0)
        m.set_day_realized_pnl(-3.0)

        alerts = m.check([])
        assert not m.globally_paused, "低于绝对金额地板不应暂停"

    @patch("src.monitor.risk_monitor.get")
    def test_cum_realized_loss_triggers_pause(self, mock_get):
        """
        场景：累计结算亏损 40 USDC / 峰值 60 USDC = 66.7% > 50%。
        期望：触发累计暂停。
        """
        mock_get.side_effect = _smart_cfg
        m = RiskMonitor(initial_balance=60.0, peak_balance_from_db=60.0)
        m.update_balance(20.0)
        m.set_day_realized_pnl(0.0)  # 今日无新亏损
        m.set_cum_realized_loss(40.0)  # 历史累计亏 40

        alerts = m.check([])
        assert m.globally_paused
        assert m.pause_reason == "cumulative"
        assert any("DB结算" in a for a in alerts)

    @patch("src.monitor.risk_monitor.get")
    def test_none_realized_pnl_falls_back_to_balance_tracking(self, mock_get):
        """
        场景：未调用 set_day_realized_pnl（None），回退到余额变化追踪。
        期望：行为与原来 update_balance() → check() 一致（兼容已有测试）。
        """
        mock_get.side_effect = _smart_cfg
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(100.0)
        m.update_balance(30.0)  # -70%，大于 50% 门槛

        # 不注入 realized PnL → 使用余额追踪（_day_realized_pnl = None）
        alerts = m.check([])
        assert m.globally_paused, "回退模式下余额下降 >50% 应触发暂停"

    @patch("src.monitor.risk_monitor.get")
    def test_profit_today_no_pause(self, mock_get):
        """
        场景：今日结算盈利 5 USDC（正数），余额也上升。
        期望：不触发暂停。
        """
        mock_get.side_effect = _smart_cfg
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(105.0)
        m.set_day_realized_pnl(5.0)  # 今日盈利
        m.set_cum_realized_loss(0.0)

        alerts = m.check([])
        assert not m.globally_paused


# ───────────────────────────────────────────────
# DB 函数集成测试
# ───────────────────────────────────────────────


class TestDbRealizedPnlFunctions:
    """get_today_settled_pnl / get_total_realized_loss 集成测试"""

    def _insert_bet(self, db_path, bet_id, pnl, resolved=True):
        """辅助：插入并结算一条投注记录。"""
        from src.utils.db import insert_paper_bet, update_paper_bet_outcome

        bet = {
            "bet_id": bet_id,
            "ts_entered": time.time() - 3600,
            "market_id": f"mkt_{bet_id}",
            "question": f"Test market {bet_id}",
            "category": "sports",
            "outcome": "YES",
            "confidence": 0.7,
            "ai_probability": 0.7,
            "market_price": 0.65,
            "ev": 0.05,
            "size_usdc": 5.0,
            "sim_balance_before": 100.0,
            "sim_balance_after": 95.0,
            "market_end_date": "2026-12-31",
            "weather_context": "",
            "ai_models": "gemini-2.0-flash-lite",
            "cycle_id": 1,
        }
        insert_paper_bet(bet, db_path=db_path)
        if resolved:
            is_win = 1 if pnl > 0 else 0
            update_paper_bet_outcome(
                bet_id=bet_id,
                resolved_outcome="YES",
                resolved_at=time.time(),
                pnl=pnl,
                payout=5.0 + pnl,
                is_win=is_win,
                db_path=db_path,
            )

    def test_today_settled_pnl_empty(self, tmp_db):
        """无结算记录时返回 0.0。"""
        result = db_mod.get_today_settled_pnl(db_path=tmp_db)
        assert result == 0.0

    def test_today_settled_pnl_sum(self, tmp_db):
        """汇总今日结算 PnL（正负混合）。"""
        self._insert_bet(tmp_db, "win1", pnl=3.0)
        self._insert_bet(tmp_db, "loss1", pnl=-2.0)
        self._insert_bet(tmp_db, "win2", pnl=1.5)
        result = db_mod.get_today_settled_pnl(db_path=tmp_db)
        assert abs(result - 2.5) < 1e-6, f"Expected 2.5, got {result}"

    def test_today_settled_pnl_open_bets_excluded(self, tmp_db):
        """未结算仓位不计入今日 PnL。"""
        self._insert_bet(tmp_db, "open1", pnl=0.0, resolved=False)
        self._insert_bet(tmp_db, "loss2", pnl=-4.0)
        result = db_mod.get_today_settled_pnl(db_path=tmp_db)
        assert abs(result - (-4.0)) < 1e-6

    def test_total_realized_loss_zero(self, tmp_db):
        """无亏损记录时返回 0.0。"""
        self._insert_bet(tmp_db, "win3", pnl=5.0)
        result = db_mod.get_total_realized_loss(db_path=tmp_db)
        assert result == 0.0

    def test_total_realized_loss_sum(self, tmp_db):
        """汇总所有已结算亏损（绝对值之和）。"""
        self._insert_bet(tmp_db, "loss3", pnl=-3.0)
        self._insert_bet(tmp_db, "loss4", pnl=-5.0)
        self._insert_bet(tmp_db, "win4", pnl=2.0)  # 盈利不计
        result = db_mod.get_total_realized_loss(db_path=tmp_db)
        assert abs(result - 8.0) < 1e-6, f"Expected 8.0, got {result}"

    def test_total_realized_loss_excludes_open(self, tmp_db):
        """未结算开仓不计入累计亏损。"""
        self._insert_bet(tmp_db, "open2", pnl=0.0, resolved=False)
        result = db_mod.get_total_realized_loss(db_path=tmp_db)
        assert result == 0.0
