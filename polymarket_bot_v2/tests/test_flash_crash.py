"""
test_flash_crash.py — T71 体育赛道闪崩止损测试
覆盖：
  1. 3分钟滑窗跌幅触发闪崩
  2. 二次确认后未反弹 → 确认止损
  3. 二次确认后反弹 → 取消止损
  4. 低价直接触发（无需确认）
  5. 非体育赛道不触发
"""

import time
from collections import deque
from unittest.mock import patch

import pytest
from src.monitor.price_monitor import PriceMonitor


def _make_monitor(**overrides):
    """创建一个配置好flash_crash的PriceMonitor实例"""
    cfg = {
        "enabled": True,
        "memory_buffer_size": 360,
        "flash_crash": {
            "enabled": True,
            "window_3min_pct": 18.0,
            "window_5min_pct": 28.0,
            "confirm_delay_seconds": 90,
            "confirm_rebound_threshold": 0.70,
            "min_price_for_trigger": 0.10,
        },
    }
    cfg.update(overrides)
    with patch("src.monitor.price_monitor.get", return_value=cfg):
        with patch.object(PriceMonitor, "_load_crossed_from_db"):
            pm = PriceMonitor()
    return pm


def _make_bet(
    bet_id="bet-001", category="sports", outcome="YES", entry=0.50, market_id="mkt1"
):
    return {
        "bet_id": bet_id,
        "category": category,
        "outcome": outcome,
        "market_price": entry,
        "market_id": market_id,
    }


def _make_market_data(market_id="mkt1", yes_price=0.40):
    return {
        market_id: {
            "outcomePrices": f"[{yes_price}, {1.0 - yes_price}]",
        }
    }


class TestFlashCrashDetection:
    """闪崩滑窗检测"""

    def test_3min_drop_triggers_pending(self):
        """3分钟内下跌≥18% → 加入待确认队列"""
        pm = _make_monitor()
        bet = _make_bet(entry=0.50)
        now = time.time()

        # 模拟3分钟前价格 0.50，现在 0.40 (20% drop)
        pm._buffers["bet-001"] = deque(
            [
                (now - 170, 0.50, 0.49, 0.51),
                (now - 10, 0.40, 0.39, 0.41),
            ]
        )

        md = _make_market_data(yes_price=0.40)
        results = pm.check_flash_crashes([bet], md)

        # 首次检测 → 进入pending，不立即返回结果
        assert len(results) == 0
        assert "bet-001" in pm._fc_pending

    def test_confirm_no_rebound_triggers_exit(self):
        """确认期后未反弹 → 返回止损指令"""
        pm = _make_monitor()
        bet = _make_bet(entry=0.50)
        now = time.time()

        # 设置为91秒前触发
        pm._fc_pending["bet-001"] = (now - 91, 0.40, 0.50)
        pm._buffers["bet-001"] = deque(
            [
                (now - 170, 0.50, 0.49, 0.51),
                (now - 10, 0.35, 0.34, 0.36),  # 继续下跌
            ]
        )

        # 当前价格0.35 < 0.40 * 0.70 = 0.28 ? No, 0.35 > 0.28
        # Actually rebound_target = trigger_price * 0.70 = 0.40 * 0.70 = 0.28
        # 0.35 ≥ 0.28 → 算作反弹！
        # 要测试未反弹，价格需要 < 0.28
        md = _make_market_data(yes_price=0.25)  # < 0.28
        results = pm.check_flash_crashes([bet], md)

        assert len(results) == 1
        assert results[0]["trigger"] == "flash_crash_confirmed"
        assert results[0]["bet_id"] == "bet-001"

    def test_confirm_rebound_cancels(self):
        """确认期后已反弹 → 取消止损"""
        pm = _make_monitor()
        bet = _make_bet(entry=0.50)
        now = time.time()

        # 91秒前触发，trigger_price=0.40
        pm._fc_pending["bet-001"] = (now - 91, 0.40, 0.50)
        pm._buffers["bet-001"] = deque(
            [
                (now - 170, 0.50, 0.49, 0.51),
                (now - 10, 0.45, 0.44, 0.46),
            ]
        )

        # 当前价格0.45 ≥ 0.40*0.70=0.28 → 反弹取消
        md = _make_market_data(yes_price=0.45)
        results = pm.check_flash_crashes([bet], md)

        assert len(results) == 0
        assert "bet-001" not in pm._fc_pending  # 已清理

    def test_low_price_direct_trigger(self):
        """价格低于min_price_for_trigger → 直接触发（无需确认）"""
        pm = _make_monitor()
        bet = _make_bet(entry=0.50)
        now = time.time()

        pm._buffers["bet-001"] = deque(
            [
                (now - 170, 0.50, 0.49, 0.51),
                (now - 10, 0.08, 0.07, 0.09),
            ]
        )

        md = _make_market_data(yes_price=0.08)  # < 0.10
        results = pm.check_flash_crashes([bet], md)

        assert len(results) == 1
        assert results[0]["trigger"] == "flash_crash_min_price"

    def test_non_sports_ignored(self):
        """非体育赛道不触发"""
        pm = _make_monitor()
        bet = _make_bet(category="weather", entry=0.50)
        now = time.time()

        pm._buffers["bet-001"] = deque(
            [
                (now - 170, 0.50, 0.49, 0.51),
                (now - 10, 0.30, 0.29, 0.31),
            ]
        )

        md = _make_market_data(yes_price=0.30)
        results = pm.check_flash_crashes([bet], md)
        assert len(results) == 0

    def test_5min_drop_triggers(self):
        """5分钟内下跌≥28% → 触发"""
        pm = _make_monitor()
        bet = _make_bet(entry=0.50)
        now = time.time()

        # 只有5分钟前的数据点（无3分钟数据）
        pm._buffers["bet-001"] = deque(
            [
                (now - 290, 0.50, 0.49, 0.51),  # ～5min ago
                (now - 10, 0.35, 0.34, 0.36),  # 30% drop
            ]
        )

        md = _make_market_data(yes_price=0.35)
        results = pm.check_flash_crashes([bet], md)

        assert len(results) == 0  # First detection → pending
        assert "bet-001" in pm._fc_pending

    def test_no_double_trigger(self):
        """已触发的bet不重复触发"""
        pm = _make_monitor()
        bet = _make_bet(entry=0.50)
        now = time.time()

        pm._fc_triggered["bet-001"] = now - 60  # 已触发
        pm._buffers["bet-001"] = deque(
            [
                (now - 170, 0.50, 0.49, 0.51),
                (now - 10, 0.05, 0.04, 0.06),
            ]
        )

        md = _make_market_data(yes_price=0.05)
        results = pm.check_flash_crashes([bet], md)
        assert len(results) == 0
