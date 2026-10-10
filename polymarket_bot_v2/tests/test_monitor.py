"""
test_monitor.py — Risk Monitor 单元测试
"""

from unittest.mock import patch

import pytest
from src.executor.order_executor import OrderResult
from src.monitor.risk_monitor import RiskMonitor
from src.signal.signal_engine import Signal


def make_signal(question="Will BTC go up?", outcome="YES", size=5.0):
    return Signal(
        market_id="test_market",
        question=question,
        outcome=outcome,
        ev=0.05,
        confidence=0.70,
        size_usdc=size,
    )


class TestRiskMonitor:
    def setup_method(self):
        self.monitor = RiskMonitor(initial_balance=1000.0)

    def test_initial_state(self):
        assert self.monitor.balance == 1000.0
        assert not self.monitor.globally_paused

    def test_update_balance_profit(self):
        self.monitor.update_balance(1050.0)
        assert self.monitor.balance == 1050.0

    @patch(
        "src.monitor.risk_monitor.get",
        side_effect=lambda k: (
            0.05
            if k == "risk.daily_loss_pause_pct"
            else ({"enabled": False} if k == "risk.smart_pause" else None)
        ),
    )
    def test_daily_loss_triggers_pause(self, _mock_get):
        # 损失 6%（阈值 5%）— smart_pause disabled → 使用原始阈值
        self.monitor.update_balance(940.0)
        alerts = self.monitor.check([])
        assert self.monitor.globally_paused
        assert any("暂停" in a for a in alerts)

    @patch(
        "src.monitor.risk_monitor.get",
        side_effect=lambda k: (
            0.05
            if k == "risk.daily_loss_pause_pct"
            else ({"enabled": False} if k == "risk.smart_pause" else None)
        ),
    )
    def test_resume_clears_pause(self, _mock_get):
        self.monitor.update_balance(940.0)
        self.monitor.check([])
        assert self.monitor.globally_paused
        self.monitor.resume()
        assert not self.monitor.globally_paused
        assert self.monitor.pause_reason is None

    def test_infer_category_weather(self):
        cat = self.monitor._infer_category("Will it rain in Paris tomorrow?")
        assert cat == "weather"

    def test_infer_category_crypto(self):
        cat = self.monitor._infer_category("Will Bitcoin price exceed $100k?")
        assert cat == "crypto"

    def test_infer_category_sports_nba(self):
        cat = self.monitor._infer_category("Will the Lakers win the NBA Finals?")
        assert cat == "sports"

    def test_infer_category_sports_vs(self):
        cat = self.monitor._infer_category("Warriors vs Celtics: Who will win?")
        assert cat == "sports"

    def test_infer_category_sports_ufc(self):
        cat = self.monitor._infer_category("Will Jon Jones win the UFC bout?")
        assert cat == "sports"

    def test_infer_category_geopolitics(self):
        cat = self.monitor._infer_category("Will the Ukraine war end this year?")
        assert cat == "geopolitics"

    def test_category_loss_streak_ignores_execution_failures(self):
        """执行失败（success=False）不应计入交易连亏。"""
        sig = make_signal(question="Will Bitcoin hit $200k?")
        failed_result = OrderResult(signal=sig, success=False, error="CLOB rejected")
        alerts = self.monitor.check([failed_result])
        assert self.monitor._category_loss.get("crypto", 0) == 0

    def test_category_loss_streak_tracks_trading_pnl(self):
        """交易实际亏损（pnl < 0）应计入连亏。"""
        sig = make_signal(question="Will Bitcoin hit $200k?")
        loss_result = OrderResult(signal=sig, success=True, tx_hash="0xABC")
        loss_result.pnl = -2.0  # 交易亏损
        alerts = self.monitor.check([loss_result])
        assert self.monitor._category_loss.get("crypto", 0) == 1

    def test_category_loss_streak_resets_on_profit(self):
        """交易盈利应重置连亏计数。"""
        sig = make_signal(question="Will Bitcoin hit $200k?")
        # 先制造连亏
        for _ in range(2):
            loss_result = OrderResult(signal=sig, success=True, tx_hash="0xL")
            loss_result.pnl = -1.0
            self.monitor.check([loss_result])
        assert self.monitor._category_loss.get("crypto", 0) == 2

        # 盈利重置
        win_result = OrderResult(signal=sig, success=True, tx_hash="0xW")
        win_result.pnl = 3.0
        self.monitor.check([win_result])
        assert self.monitor._category_loss.get("crypto", 0) == 0

    def test_dry_run_does_not_affect_loss_streak(self):
        """DRY RUN 模拟单不应影响连亏计数。"""
        sig = make_signal(question="Will Bitcoin hit $200k?")
        dry_result = OrderResult(signal=sig, success=True, tx_hash="DRY_RUN")
        self.monitor.check([dry_result])
        assert self.monitor._category_loss.get("crypto", 0) == 0

    @patch("src.utils.db.get_total_realized_pnl", return_value=0.0)
    @patch("src.monitor.risk_monitor.get")
    def test_deposit_jump_does_not_trigger_withdraw_alert(self, mock_get, _mock_pnl):
        """T179-A: 用户充值不应被误判为盈利并触发提现告警。"""
        mock_get.side_effect = lambda k: {
            "risk.withdraw_trigger_pct": 0.30,
            "risk.daily_loss_pause_pct": 0.50,
            "risk.cumulative_loss_pause_pct": 0.50,
            "risk.deposit_detection_threshold_usdc": 15.0,
            "risk.category_pause_on_loss_streak": 3,
        }.get(k)
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(100.0)
        m.update_balance(193.56, nlv=193.56)  # 充值 +$93.56
        alerts = m.check([])
        assert not any("[提现]" in a for a in alerts)

    @patch("src.utils.db.get_total_realized_pnl", return_value=35.0)
    @patch("src.monitor.risk_monitor.get")
    def test_realized_profit_still_triggers_withdraw_alert(self, mock_get, _mock_pnl):
        """T179-A: 真实已结算利润达到阈值时，仍应正常触发提现提醒。"""
        mock_get.side_effect = lambda k: {
            "risk.withdraw_trigger_pct": 0.30,
            "risk.daily_loss_pause_pct": 0.50,
            "risk.cumulative_loss_pause_pct": 0.50,
            "risk.deposit_detection_threshold_usdc": 15.0,
            "risk.category_pause_on_loss_streak": 3,
        }.get(k)
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(135.0, nlv=135.0)
        alerts = m.check([])
        assert any("[提现]" in a for a in alerts)
        assert any("DB结算" in a for a in alerts)


class TestSmartPause:
    """智能暂停：绝对最小损失地板 + 小余额自适应阈值。"""

    def _smart_get(self, k):
        """Mock config: smart_pause enabled, small balance < $20."""
        cfg = {
            "risk.smart_pause": {
                "enabled": True,
                "min_absolute_daily_loss_usdc": 1.0,
                "min_absolute_cumulative_loss_usdc": 3.0,
                "small_balance_threshold_usdc": 20.0,
                "small_balance_daily_pause_pct": 0.25,
                "small_balance_cumulative_pause_pct": 0.50,
            },
            "risk.daily_loss_pause_pct": 0.20,
            "risk.cumulative_loss_pause_pct": 0.40,
            "risk.withdraw_trigger_pct": 0.50,
            "risk.category_pause_on_loss_streak": 3,
        }
        return cfg.get(k)

    @patch("src.monitor.risk_monitor.get")
    def test_small_loss_below_floor_no_pause(self, mock_get):
        """$0.50 loss on $10 balance: 5% but below $1.0 floor -> no pause."""
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=10.0)
        m.update_balance(10.0)
        m.update_balance(9.5)  # -$0.50 = 5%
        alerts = m.check([])
        assert not m.globally_paused, "Should NOT pause: loss $0.50 < floor $1.0"

    @patch("src.monitor.risk_monitor.get")
    def test_small_balance_uses_relaxed_pct(self, mock_get):
        """$10 balance, $1.5 loss: 1.5/8.5=17.6% < small_balance 25% -> no pause."""
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=10.0)
        m.update_balance(10.0)
        m.update_balance(8.5)  # -$1.50, loss/balance = 1.5/8.5 ≈ 17.6%
        alerts = m.check([])
        assert not m.globally_paused, "17.6% < 25% threshold for small balance"

    @patch("src.monitor.risk_monitor.get")
    def test_small_balance_large_loss_triggers_pause(self, mock_get):
        """$10 balance, $3 loss (30%): above 25% + above $1.0 floor -> pause."""
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=10.0)
        m.update_balance(10.0)
        m.update_balance(7.0)  # -$3 = 30%
        alerts = m.check([])
        assert m.globally_paused
        assert m.pause_reason == "daily"

    @patch("src.monitor.risk_monitor.get")
    def test_large_balance_uses_normal_thresholds(self, mock_get):
        """$100 balance (>$20): uses normal 20% daily threshold."""
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(100.0)
        m.update_balance(78.0)  # -$22 = 22% > 20%
        alerts = m.check([])
        assert m.globally_paused

    @patch("src.monitor.risk_monitor.get")
    def test_peak_balance_from_db(self, mock_get):
        """peak_balance_from_db prevents cumulative pause on restart."""
        mock_get.side_effect = self._smart_get
        # User deposited $30, DB peak was $30, but current is $10
        m = RiskMonitor(initial_balance=10.0, peak_balance_from_db=30.0)
        assert m._peak_balance == 30.0
        m.update_balance(10.0)
        # cum_loss = 0 still (just initialized)
        m.update_balance(7.0)  # -$3, cum_loss = $3
        alerts = m.check([])
        # cum_loss/peak = $3/$30 = 10% < 50% -> no cumulative pause
        assert not m.globally_paused or m.pause_reason != "cumulative"

    @patch("src.monitor.risk_monitor.get")
    def test_daily_auto_resume_on_day_change(self, mock_get):
        """Daily pause auto-resumes on new UTC day."""
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(100.0)
        m.update_balance(70.0)  # -$30 = 30%
        m.check([])
        assert m.globally_paused
        assert m.pause_reason == "daily"

        # Simulate day change
        from datetime import timedelta

        m._day_date = m._day_date - timedelta(days=1)
        m.update_balance(70.0)
        assert not m.globally_paused, "Should auto-resume on day change"

    @patch("src.monitor.risk_monitor.get")
    def test_cumulative_pause_no_auto_resume(self, mock_get):
        """Cumulative pause does NOT auto-resume on day change."""
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(100.0)
        # Produce cumulative loss > 40%
        m.update_balance(70.0)  # -$30
        m._cum_loss = 45.0  # Force cumulative past threshold
        m.check([])
        assert m.globally_paused
        assert m.pause_reason == "cumulative"

        # Simulate day change
        from datetime import timedelta

        m._day_date = m._day_date - timedelta(days=1)
        m.update_balance(70.0)
        assert m.globally_paused, "Cumulative pause should persist across days"


class TestNLVRiskMonitor:
    """NLV（净清算价值）风控计算：确保使用 NLV 而非 raw cash 进行百分比判断。"""

    def _smart_get(self, k):
        cfg = {
            "risk.smart_pause": {
                "enabled": True,
                "min_absolute_daily_loss_usdc": 5.0,
                "min_absolute_cumulative_loss_usdc": 10.0,
                "small_balance_threshold_usdc": 20.0,
                "small_balance_daily_pause_pct": 0.25,
                "small_balance_cumulative_pause_pct": 0.50,
            },
            "risk.daily_loss_pause_pct": 0.20,
            "risk.cumulative_loss_pause_pct": 0.40,
            "risk.withdraw_trigger_pct": 0.50,
            "risk.category_pause_on_loss_streak": 3,
        }
        return cfg.get(k)

    @patch("src.monitor.risk_monitor.get")
    def test_daily_loss_uses_nlv_denominator(self, mock_get):
        """日亏损百分比用 NLV 分母：$10.93 / $80 = 13.7% < 20% → 不暂停。
        Bug 修复前：$10.93 / $26.81(raw cash) = 40.8% → 误触暂停。
        """
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(100.0, nlv=100.0)
        # 余额从 100 → 26.81（投注部署），NLV 仍有 80（含持仓）
        m.update_balance(26.81, nlv=80.0)
        # 注入 DB 结算：今日亏损 $10.93
        m.set_day_realized_pnl(-10.93)
        alerts = m.check([])
        # 10.93 / 80 = 13.7% < 20% → 不应暂停
        assert not m.globally_paused, (
            f"Should NOT pause: $10.93/$80=13.7% < 20%. "
            f"NLV={m.nlv}, balance={m.balance}"
        )

    @patch("src.monitor.risk_monitor.get")
    def test_daily_loss_triggers_with_nlv_when_real_loss(self, mock_get):
        """NLV 也低时（真实亏损），仍正确触发暂停。"""
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(100.0, nlv=100.0)
        # 真实亏损：余额 50，NLV 也只有 50（持仓也跌了）
        m.update_balance(50.0, nlv=50.0)
        m.set_day_realized_pnl(-15.0)
        alerts = m.check([])
        # 15 / 50 = 30% > 20% → 应暂停
        # NLV drawdown = (100-50)/100 = 50% > 40% → cumulative 也触发(覆盖 daily)
        assert m.globally_paused

    @patch("src.monitor.risk_monitor.get")
    def test_cumulative_nlv_drawdown(self, mock_get):
        """累计回撤用 NLV drawdown：(peak_nlv - nlv) / peak_nlv。"""
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(100.0, nlv=100.0)
        # NLV 从 100 → 80，回撤 20% < 40% → 不暂停
        m.update_balance(27.0, nlv=80.0)
        # 注入 DB 结算：今日无已结算亏损（仅是资金部署）
        m.set_day_realized_pnl(0.0)
        alerts = m.check([])
        assert (
            not m.globally_paused
        ), "NLV drawdown 20% < 40% threshold, should NOT pause"

    @patch("src.monitor.risk_monitor.get")
    def test_cumulative_nlv_drawdown_triggers(self, mock_get):
        """NLV 真实回撤超过阈值时触发暂停。"""
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(100.0, nlv=100.0)
        # NLV 从 100 → 50，回撤 50% > 40% → 暂停
        m.update_balance(20.0, nlv=50.0)
        alerts = m.check([])
        assert m.globally_paused
        assert m.pause_reason == "cumulative"

    @patch("src.monitor.risk_monitor.get")
    def test_peak_nlv_tracking(self, mock_get):
        """peak_nlv 随 NLV 上涨更新，用于回撤计算。"""
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(100.0, nlv=100.0)
        m.update_balance(90.0, nlv=120.0)  # NLV 上涨到 120
        assert m._peak_nlv == 120.0
        # NLV 从 120 → 85，回撤 29.2% < 40% → 不暂停
        m.update_balance(30.0, nlv=85.0)
        alerts = m.check([])
        assert not m.globally_paused, "35/120 = 29.2% < 40%"

    @patch("src.monitor.risk_monitor.get")
    def test_nlv_prevents_small_balance_false_positive(self, mock_get):
        """raw cash < $20 但 NLV > $20 → 应使用正常阈值，非小余额阈值。"""
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=100.0)
        m.update_balance(100.0, nlv=100.0)
        # 余额 $15 (< $20 小余额阈值) 但 NLV = $80
        m.update_balance(15.0, nlv=80.0)
        # NLV=$80 > small_balance_threshold=$20 → 使用正常 20% 阈值
        m.set_day_realized_pnl(-10.0)
        alerts = m.check([])
        # 10/80 = 12.5% < 20% → 不暂停（如果错误使用 raw cash 的小余额阈值 25% 也不会暂停，
        # 但关键是分母用 NLV）
        assert not m.globally_paused

    @patch("src.monitor.risk_monitor.get")
    def test_nlv_property(self, mock_get):
        """nlv 属性正确返回当前 NLV。"""
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=50.0)
        assert m.nlv == 50.0  # default = initial_balance
        m.update_balance(30.0, nlv=75.0)
        assert m.nlv == 75.0
        assert m.balance == 30.0  # raw cash unchanged

    @patch("src.monitor.risk_monitor.get")
    def test_production_scenario(self, mock_get):
        """复现生产问题：余额 $26.81, NLV ~$80, 日亏 $10.93 → 不应暂停。

        修复前：10.93/26.81 = 40.8% → 误触 25% 暂停
        修复后：10.93/80 = 13.7% → 远低于 20% 阈值
        """
        mock_get.side_effect = self._smart_get
        m = RiskMonitor(initial_balance=100.0, peak_balance_from_db=101.73)
        m.update_balance(100.0, nlv=100.0)
        # 模拟投注后余额下降但 NLV 稳定
        m.update_balance(26.81, nlv=80.0)
        m.set_day_realized_pnl(-10.93)
        m.set_cum_realized_loss(56.42)  # DB gross losses（不应影响 NLV 回撤判断）
        alerts = m.check([])
        # Daily: 10.93/80 = 13.7% < 20% → pass
        # Cumulative: NLV drawdown = (101.73-80)/101.73 = 21.4% < 40% → pass
        assert not m.globally_paused, (
            f"Production scenario: should NOT pause. "
            f"Daily: {10.93/80:.1%}, Cum drawdown: {(101.73-80)/101.73:.1%}"
        )
