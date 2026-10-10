"""
test_risk_auto_recovery.py — T173-追加: 风控智能自动恢复测试

测试目标：
  1. balance_floor 暂停 → 余额恢复后自动解除
  2. daily 暂停 → UTC跨日后自动解除
  3. cumulative 暂停 → 回撤降低后自动解除
  4. emergency_freeze → 绝不自动恢复
  5. manual 暂停（pause_reason=None/"manual"）→ 绝不自动恢复
  6. auto_recovery_enabled=false → 禁用自动恢复
  7. buffer 机制：余额刚好等于 floor 不恢复，超过 floor*(1+buffer) 才恢复
"""

from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest
from src.monitor.risk_monitor import RiskMonitor

# ───────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────


def _cfg(overrides=None):
    """返回 mock config get 函数，支持覆盖特定参数。"""
    defaults = {
        "risk.daily_loss_pause_pct": 0.50,
        "risk.cumulative_loss_pause_pct": 0.40,
        "risk.min_absolute_daily_loss_usdc": 5.0,
        "risk.min_absolute_cumulative_loss_usdc": 10.0,
        "risk.balance_floor_usdc": 0.5,
        "risk.withdraw_trigger_pct": 0.30,
        "risk.category_pause_on_loss_streak": 3,
        "risk.emergency_freeze": False,
        "risk.auto_recovery_enabled": True,
        "risk.auto_recovery_buffer_pct": 0.10,
    }
    if overrides:
        defaults.update(overrides)

    def _get(key):
        return defaults.get(key)

    return _get


# ───────────────────────────────────────────────
# Tests
# ───────────────────────────────────────────────


class TestAutoRecoveryBalanceFloor:
    """balance_floor 暂停的自动恢复场景。"""

    @patch("src.monitor.risk_monitor.get", side_effect=_cfg())
    @patch("src.monitor.risk_monitor.logger")
    def test_balance_floor_auto_recovery_when_balance_restored(
        self, mock_log, mock_get
    ):
        """余额从0.38恢复到1.58 → 自动恢复。"""
        rm = RiskMonitor(initial_balance=10.0)
        rm._globally_paused = True
        rm._pause_reason = "balance_floor"
        rm._balance = 0.38

        result = rm.check_auto_recovery(current_balance=1.58)

        assert result is True
        assert rm._globally_paused is False
        assert rm._pause_reason is None

    @patch("src.monitor.risk_monitor.get", side_effect=_cfg())
    @patch("src.monitor.risk_monitor.logger")
    def test_balance_floor_no_recovery_below_buffer(self, mock_log, mock_get):
        """余额=0.52，低于 floor(0.5) * 1.10 = 0.55 → 不恢复。"""
        rm = RiskMonitor(initial_balance=10.0)
        rm._globally_paused = True
        rm._pause_reason = "balance_floor"
        rm._balance = 0.38

        result = rm.check_auto_recovery(current_balance=0.52)

        assert result is False
        assert rm._globally_paused is True

    @patch("src.monitor.risk_monitor.get", side_effect=_cfg())
    @patch("src.monitor.risk_monitor.logger")
    def test_balance_floor_recovery_at_exact_threshold(self, mock_log, mock_get):
        """余额=0.55，等于 floor(0.5) * 1.10 = 0.55 → 恢复。"""
        rm = RiskMonitor(initial_balance=10.0)
        rm._globally_paused = True
        rm._pause_reason = "balance_floor"
        rm._balance = 0.38

        result = rm.check_auto_recovery(current_balance=0.55)

        assert result is True
        assert rm._globally_paused is False

    @patch(
        "src.monitor.risk_monitor.get",
        side_effect=_cfg({"risk.auto_recovery_buffer_pct": 0.20}),
    )
    @patch("src.monitor.risk_monitor.logger")
    def test_balance_floor_custom_buffer(self, mock_log, mock_get):
        """自定义 buffer=20%，floor=0.5 → 需 0.60 才恢复。"""
        rm = RiskMonitor(initial_balance=10.0)
        rm._globally_paused = True
        rm._pause_reason = "balance_floor"

        # 0.58 < 0.60 → 不恢复
        assert rm.check_auto_recovery(current_balance=0.58) is False
        # 0.60 = 0.60 → 恢复
        assert rm.check_auto_recovery(current_balance=0.60) is True


class TestAutoRecoveryDailyLoss:
    """daily_loss 暂停的自动恢复场景。"""

    @patch("src.monitor.risk_monitor.get", side_effect=_cfg())
    @patch("src.monitor.risk_monitor.logger")
    def test_daily_auto_recovery_on_day_change(self, mock_log, mock_get):
        """UTC 跨日 → 日亏损暂停自动恢复。"""
        rm = RiskMonitor(initial_balance=100.0)
        rm._globally_paused = True
        rm._pause_reason = "daily"
        rm._day_pnl = -60.0
        rm._day_date = date(2026, 4, 8)  # 昨天

        result = rm.check_auto_recovery(current_balance=40.0)

        assert result is True
        assert rm._globally_paused is False
        assert rm._day_pnl == 0.0  # 日盈亏已重置

    @patch("src.monitor.risk_monitor.get", side_effect=_cfg())
    @patch("src.monitor.risk_monitor.logger")
    def test_daily_no_recovery_same_day(self, mock_log, mock_get):
        """同天 → 不恢复。"""
        rm = RiskMonitor(initial_balance=100.0)
        rm._globally_paused = True
        rm._pause_reason = "daily"
        rm._day_date = datetime.now(timezone.utc).date()  # 今天

        result = rm.check_auto_recovery(current_balance=40.0)

        assert result is False
        assert rm._globally_paused is True


class TestAutoRecoveryCumulative:
    """cumulative_loss 暂停的自动恢复场景。"""

    @patch("src.monitor.risk_monitor.get", side_effect=_cfg())
    @patch("src.monitor.risk_monitor.logger")
    def test_cumulative_recovery_when_loss_decreased(self, mock_log, mock_get):
        """累计损失降低到阈值以下 → 自动恢复。"""
        rm = RiskMonitor(initial_balance=100.0, peak_balance_from_db=100.0)
        rm._globally_paused = True
        rm._pause_reason = "cumulative"
        rm._cum_loss = 2.0  # 远低于 min_absolute_cumulative_loss_usdc=10
        rm._peak_balance = 100.0

        result = rm.check_auto_recovery(current_balance=98.0)

        assert result is True
        assert rm._globally_paused is False

    @patch("src.monitor.risk_monitor.get", side_effect=_cfg())
    @patch("src.monitor.risk_monitor.logger")
    def test_cumulative_no_recovery_still_above_threshold(self, mock_log, mock_get):
        """累计损失仍高于阈值 → 不恢复。"""
        rm = RiskMonitor(initial_balance=100.0, peak_balance_from_db=100.0)
        rm._globally_paused = True
        rm._pause_reason = "cumulative"
        rm._cum_loss = 45.0  # 45/100 = 45% > 40%*0.8=32%
        rm._peak_balance = 100.0

        result = rm.check_auto_recovery(current_balance=55.0)

        assert result is False
        assert rm._globally_paused is True


class TestAutoRecoveryExclusions:
    """绝不自动恢复的场景。"""

    @patch(
        "src.monitor.risk_monitor.get",
        side_effect=_cfg({"risk.emergency_freeze": True}),
    )
    @patch("src.monitor.risk_monitor.logger")
    def test_emergency_freeze_never_auto_recovers(self, mock_log, mock_get):
        """紧急冻结 → 绝不自动恢复。"""
        rm = RiskMonitor(initial_balance=100.0)
        rm._globally_paused = True
        rm._pause_reason = "balance_floor"
        rm._balance = 0.38

        result = rm.check_auto_recovery(current_balance=50.0)

        assert result is False
        assert rm._globally_paused is True

    @patch("src.monitor.risk_monitor.get", side_effect=_cfg())
    @patch("src.monitor.risk_monitor.logger")
    def test_manual_pause_never_auto_recovers(self, mock_log, mock_get):
        """手动暂停 (reason=None) → 绝不自动恢复。"""
        rm = RiskMonitor(initial_balance=100.0)
        rm._globally_paused = True
        rm._pause_reason = None

        result = rm.check_auto_recovery(current_balance=50.0)

        assert result is False

    @patch("src.monitor.risk_monitor.get", side_effect=_cfg())
    @patch("src.monitor.risk_monitor.logger")
    def test_manual_pause_reason_manual(self, mock_log, mock_get):
        """手动暂停 (reason='manual') → 绝不自动恢复。"""
        rm = RiskMonitor(initial_balance=100.0)
        rm._globally_paused = True
        rm._pause_reason = "manual"

        result = rm.check_auto_recovery(current_balance=50.0)

        assert result is False

    @patch(
        "src.monitor.risk_monitor.get",
        side_effect=_cfg({"risk.auto_recovery_enabled": False}),
    )
    @patch("src.monitor.risk_monitor.logger")
    def test_auto_recovery_disabled(self, mock_log, mock_get):
        """auto_recovery_enabled=false → 禁用自动恢复。"""
        rm = RiskMonitor(initial_balance=100.0)
        rm._globally_paused = True
        rm._pause_reason = "balance_floor"
        rm._balance = 0.38

        result = rm.check_auto_recovery(current_balance=50.0)

        assert result is False
        assert rm._globally_paused is True


class TestAutoRecoveryEdgeCases:
    """边缘场景。"""

    @patch("src.monitor.risk_monitor.get", side_effect=_cfg())
    @patch("src.monitor.risk_monitor.logger")
    def test_not_paused_returns_false(self, mock_log, mock_get):
        """未暂停 → 直接返回 False。"""
        rm = RiskMonitor(initial_balance=100.0)
        rm._globally_paused = False

        result = rm.check_auto_recovery(current_balance=50.0)

        assert result is False

    @patch("src.monitor.risk_monitor.get", side_effect=_cfg())
    @patch("src.monitor.risk_monitor.logger")
    def test_recovery_resets_peak(self, mock_log, mock_get):
        """balance_floor 恢复后 peak 重置为当前值（防止即时重触发）。"""
        rm = RiskMonitor(initial_balance=100.0, peak_balance_from_db=100.0)
        rm._globally_paused = True
        rm._pause_reason = "balance_floor"
        rm._balance = 0.38
        rm._nlv = 5.0  # 当前 NLV
        rm._peak_nlv = 100.0

        rm.check_auto_recovery(current_balance=5.0)

        assert rm._peak_nlv == 5.0  # peak 重置为当前 NLV
        assert rm._peak_balance == 5.0

    @patch("src.monitor.risk_monitor.get", side_effect=_cfg())
    @patch("src.monitor.risk_monitor.logger")
    def test_uses_internal_balance_when_no_arg(self, mock_log, mock_get):
        """不传 current_balance → 使用内部 _balance。"""
        rm = RiskMonitor(initial_balance=10.0)
        rm._globally_paused = True
        rm._pause_reason = "balance_floor"
        rm._balance = 2.0  # > 0.55

        result = rm.check_auto_recovery()

        assert result is True

    @patch("src.monitor.risk_monitor.get", side_effect=_cfg())
    @patch("src.monitor.risk_monitor.logger")
    def test_telegram_notification_on_recovery(self, mock_log, mock_get):
        """自动恢复时发送 Telegram 通知。"""
        rm = RiskMonitor(initial_balance=10.0)
        rm._globally_paused = True
        rm._pause_reason = "balance_floor"
        rm._balance = 0.38

        with patch("src.utils.telegram_notifier.get_notifier") as mock_notifier:
            mock_instance = mock_notifier.return_value
            rm.check_auto_recovery(current_balance=5.0)
            mock_instance.notify_risk_alert.assert_called_once()
            call_arg = mock_instance.notify_risk_alert.call_args[0][0]
            assert "自动恢复" in call_arg
            assert "balance_floor" in call_arg
