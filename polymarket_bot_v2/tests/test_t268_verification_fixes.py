"""
test_cn_verification_fixes.py — 城市NO策略 验证阶段发现的 BUG 修复测试

覆盖:
  1. BUG-CRITICAL-1: outcome_tracker 止损路径 config key 修正 (profit_take → profit_taking)
  2. BUG-CRITICAL-2: src/__init__.py 防御性导入 (openai 缺失不影响其他模块)
  3. BUG-MEDIUM-3: _calc_size 中 EV 扣除双边 taker 手续费
  4. BUG-MEDIUM-5: _calc_size 使用 Quarter-Kelly (0.25) 替代 Half-Kelly (0.5)
"""

import math
from unittest.mock import MagicMock, patch

import pytest
from src.signal.signal_engine import SignalEngine
from src.utils.config_loader import get

# ---------------------------------------------------------------------------
# 1. BUG-CRITICAL-1: stop-loss min_hold 正确读取 profit_taking 配置
# ---------------------------------------------------------------------------


class TestStopLossMinHoldConfigKey:
    """outcome_tracker.check_stop_loss_exits 必须从 get('profit_taking') 读取
    min_hold_minutes，而非错误的 get('profit_take')。"""

    @patch("src.simulation.outcome_tracker.get")
    def test_stop_loss_reads_profit_taking_section(self, mock_get):
        """验证止损路径使用正确的 config key='profit_taking'"""
        import time

        from src.simulation.outcome_tracker import OutcomeTracker

        call_log: list[str] = []

        def _cfg(key, default=None):
            call_log.append(key)
            configs = {
                "stop_loss": {
                    "standard_stop_pct": 0.35,
                    "force_stop_pct": 0.50,
                    "stoploss_mode": {"default": "market"},
                },
                "profit_taking": {"min_hold_minutes": 60},
                "sports_live": {
                    "game_window_hours": 4.0,
                    "post_expiry_window_hours": 2.0,
                    "floor_price": 0.10,
                    "standard_stop_pct": 0.45,
                    "reversal_exit_price": 0.15,
                },
            }
            return configs.get(key, default)

        mock_get.side_effect = _cfg

        tracker = OutcomeTracker.__new__(OutcomeTracker)
        tracker._exit_policies = []
        tracker._fetch_market_statuses = MagicMock(return_value={})

        bet = {
            "bet_id": "test_min_hold",
            "market_id": "mkt1",
            "question": "test?",
            "category": "weather",
            "outcome": "YES",
            "market_price": 0.50,
            "ts_entered": time.time() - 100,  # 100秒前入场
            "market_end_date": "",
        }

        tracker.check_stop_loss_exits([bet])

        # 验证读取了正确的 config key
        assert "profit_taking" in call_log, (
            "stop-loss 路径必须从 'profit_taking' 读取 min_hold_minutes，"
            f"实际读取的 keys: {call_log}"
        )
        # 验证没有读取错误的 key
        assert (
            "profit_take" not in call_log
        ), "stop-loss 路径不应读取已废弃的 'profit_take' key"

    @patch("src.simulation.outcome_tracker.get")
    def test_min_hold_60min_protects_new_bet(self, mock_get):
        """min_hold_minutes=60 时，入场<60分钟的仓位应跳过止损评估"""
        import time

        from src.simulation.outcome_tracker import OutcomeTracker

        def _cfg(key, default=None):
            configs = {
                "stop_loss": {
                    "standard_stop_pct": 0.35,
                    "force_stop_pct": 0.50,
                    "stoploss_mode": {"default": "market"},
                },
                "profit_taking": {"min_hold_minutes": 60},
                "sports_live": {
                    "game_window_hours": 4.0,
                    "post_expiry_window_hours": 2.0,
                    "floor_price": 0.10,
                    "standard_stop_pct": 0.45,
                    "reversal_exit_price": 0.15,
                },
            }
            return configs.get(key, default)

        mock_get.side_effect = _cfg

        tracker = OutcomeTracker.__new__(OutcomeTracker)
        tracker._exit_policies = []
        tracker._fetch_market_statuses = MagicMock(
            return_value={"mkt1": {"outcomePrices": '["0.20","0.80"]'}}
        )

        # 仓位30分钟前入场，min_hold=60min → 应被保护
        bet = {
            "bet_id": "test_protected",
            "market_id": "mkt1",
            "question": "test?",
            "category": "weather",
            "outcome": "YES",
            "market_price": 0.50,
            "ts_entered": time.time() - 1800,  # 30分钟前
            "market_end_date": "",
        }

        stops = tracker.check_stop_loss_exits([bet])
        # 30分钟 < 60分钟 min_hold → 不应触发止损
        assert len(stops) == 0, "入场30分钟 < min_hold 60分钟，不应触发止损"


# ---------------------------------------------------------------------------
# 2. BUG-CRITICAL-2: src/__init__.py 防御性导入
# ---------------------------------------------------------------------------


class TestDefensiveImport:
    """src/__init__.py 应在 openai 缺失时优雅降级"""

    def test_src_init_has_try_except_for_signal(self):
        """验证 src/__init__.py 对 SignalEngine 导入有 try/except 保护"""
        import ast
        import os

        init_path = os.path.join(os.path.dirname(__file__), "..", "src", "__init__.py")
        with open(init_path) as f:
            source = f.read()

        tree = ast.parse(source)
        has_try = any(isinstance(node, ast.Try) for node in ast.walk(tree))
        assert has_try, (
            "src/__init__.py 必须有 try/except 保护 SignalEngine 导入，"
            "防止 openai 缺失时导致所有 src.* 导入失败"
        )


# ---------------------------------------------------------------------------
# 3. BUG-MEDIUM-3: _calc_size 扣除双边 taker 手续费
# ---------------------------------------------------------------------------


class TestCalcSizeFeeDeduction:
    """T38: _calc_size 使用标准 Fractional Kelly，费用按 Polymarket 官方公式内扣"""

    def setup_method(self):
        self.engine = SignalEngine.__new__(SignalEngine)
        self.engine._evolver = None

    def test_fee_deduction_reduces_position(self):
        """T38: fee 内扣 → edge 更小 → 仓位更小"""
        _real_get = get

        def _no_fixed(key, *a, **kw):
            if key == "paper_trading.fixed_bet_size_usdc":
                return None
            if key == "paper_trading.taker_fee_rate_default":
                return 0.04
            return _real_get(key, *a, **kw)

        with patch("src.signal.signal_engine.get", side_effect=_no_fixed):
            # p_true=0.65, entry=0.55
            # fee = 0.04 * 0.55 * 0.45 = 0.0099
            # edge = 0.65 - 0.55 - 0.0099 = 0.0901
            # win_payoff = 1 - 0.55 - 0.0099 = 0.4401
            # f* = 0.0901 / 0.4401 = 0.2047
            # f = 0.25 * 0.2047 = 0.0512
            # size = 1000 * 0.0512 = 51.17
            size = self.engine._calc_size(balance=1000.0, p_true=0.65, entry_price=0.55)
            fee = 0.04 * 0.55 * 0.45
            edge = 0.65 - 0.55 - fee
            win_payoff = 1.0 - 0.55 - fee
            expected_pct = 0.25 * (edge / win_payoff)
            expected_size = round(1000.0 * expected_pct, 2)
            assert (
                abs(size - expected_size) < 0.5
            ), f"扣费后仓位应约为 ${expected_size}，实际为 ${size}"

    def test_negative_net_ev_gives_minimum(self):
        """当 edge ≤ 0 时 → size = min_bet"""
        _real_get = get

        def _no_fixed(key, *a, **kw):
            if key == "paper_trading.fixed_bet_size_usdc":
                return None
            if key == "paper_trading.taker_fee_rate_default":
                return 0.20
            return _real_get(key, *a, **kw)

        with patch("src.signal.signal_engine.get", side_effect=_no_fixed):
            # p_true=0.52, entry=0.50 → edge tiny, fee=0.20*0.50*0.50=0.05
            # edge = 0.52 - 0.50 - 0.05 = -0.03 → min_bet
            size = self.engine._calc_size(balance=1000.0, p_true=0.52, entry_price=0.50)
            min_bet = _real_get("executor.min_bet_usdc") or 1.0
            assert size == min_bet, f"edge<0 时仓位应为最小值 {min_bet}，实际为 {size}"


# ---------------------------------------------------------------------------
# 4. BUG-MEDIUM-5: Quarter-Kelly (0.25) 替代 Half-Kelly (0.5)
# ---------------------------------------------------------------------------


class TestQuarterKelly:
    """T38: _calc_size 使用标准 Fractional Kelly (kelly_fraction=0.25)"""

    def setup_method(self):
        self.engine = SignalEngine.__new__(SignalEngine)
        self.engine._evolver = None

    def test_quarter_kelly_sizing(self):
        """验证 kelly_fraction=0.25 正确应用到标准 Kelly 公式"""

        def _mock_get(key, *a, **kw):
            mapping = {
                "paper_trading.fixed_bet_size_usdc": None,
                "paper_trading.taker_fee_rate_default": 0.0,
                "risk.max_single_bet_pct": 0.10,
                "executor.min_bet_usdc": 1.0,
                "signal": {"kelly_fraction": 0.25},
            }
            if key in mapping:
                return mapping[key]
            return None

        with patch("src.signal.signal_engine.get", side_effect=_mock_get):
            # p_true=0.70, entry_price=0.50, fee=0 (rate=0)
            # edge = 0.70 - 0.50 = 0.20
            # win_payoff = 1 - 0.50 = 0.50
            # f* = 0.20 / 0.50 = 0.40
            # f = 0.25 * 0.40 = 0.10
            # pct = min(0.10, 0.10) = 0.10
            # size = 10000 * 0.10 = 1000.0
            size = self.engine._calc_size(
                balance=10000.0, p_true=0.70, entry_price=0.50
            )
            edge = 0.70 - 0.50
            win_payoff = 1.0 - 0.50
            expected_pct = min(0.25 * (edge / win_payoff), 0.10)
            expected_size = round(10000.0 * expected_pct, 2)
            assert (
                abs(size - expected_size) < 0.5
            ), f"Quarter-Kelly 仓位应为 ${expected_size}，实际为 ${size}"

    def test_quarter_kelly_is_half_of_half_kelly(self):
        """Quarter-Kelly (0.25) 仓位应为 Half-Kelly (0.50) 的一半"""

        def _mock_get(key, *a, **kw):
            mapping = {
                "paper_trading.fixed_bet_size_usdc": None,
                "paper_trading.taker_fee_rate_default": 0.0,
                "risk.max_single_bet_pct": 0.50,  # 大上限，避免截断
                "executor.min_bet_usdc": 1.0,
                "signal": {"kelly_fraction": 0.25},
            }
            if key in mapping:
                return mapping[key]
            return None

        with patch("src.signal.signal_engine.get", side_effect=_mock_get):
            # p_true=0.60, entry_price=0.52, fee=0
            # edge = 0.08, win_payoff = 0.48
            # f* = 0.08/0.48 = 0.1667
            # Quarter f = 0.25 * 0.1667 = 0.04167
            # Half f = 0.50 * 0.1667 = 0.08333
            size = self.engine._calc_size(balance=5000.0, p_true=0.60, entry_price=0.52)
            edge = 0.08
            win_payoff = 0.48
            half_kelly_size = 5000.0 * 0.50 * (edge / win_payoff)
            quarter_kelly_size = 5000.0 * 0.25 * (edge / win_payoff)
            expected = max(round(quarter_kelly_size, 2), 1.0)
            assert (
                abs(size - expected) < 0.5
            ), f"Quarter-Kelly (${size}) 应为 Half-Kelly (${half_kelly_size:.2f}) 的一半 (${expected})"

    def test_fixed_size_mode_ignores_kelly(self):
        """固定注额模式下，不受 Kelly 系数影响"""
        _real_get = get

        def _fixed_mode(key, *a, **kw):
            if key == "paper_trading.fixed_bet_size_usdc":
                return 5.0
            return _real_get(key, *a, **kw)

        with patch("src.signal.signal_engine.get", side_effect=_fixed_mode):
            size = self.engine._calc_size(balance=1000.0, p_true=0.65, entry_price=0.55)
            assert size == 5.0, f"固定注额模式应返回 $5.0，实际为 ${size}"
