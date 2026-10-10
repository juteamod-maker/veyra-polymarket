"""
test_monitor_positions.py — main.monitor_positions() 单元测试
覆盖：止盈/止损调用链、DRY_RUN vs 实盘卖出逻辑、异常处理、智能补卖
"""

from unittest.mock import MagicMock, patch

import pytest


def _make_bet(
    bet_id="b1",
    market_id="mkt_001",
    outcome="YES",
    price=0.5,
    size=2.0,
    matched_shares=10.0,
):
    return {
        "bet_id": bet_id,
        "market_id": market_id,
        "outcome": outcome,
        "market_price": price,
        "size_usdc": size,
        "matched_shares": matched_shares,
        "question": "Test?",
        "exit_price": 0.7,
        "trigger": "profit_take",
    }


class TestMonitorPositions:
    """monitor_positions() 函数级测试。"""

    @patch("main._get_components")
    @patch("main.get_open_paper_bets", return_value=[])
    def test_no_open_bets_returns_early(self, mock_bets, mock_comp):
        """无开仓时直接返回，不调用止盈/止损。"""
        from main import monitor_positions

        monitor_positions()
        mock_bets.assert_called_once()

    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_dry_run_no_sell_called(self, mock_get, mock_bets, mock_comp):
        """DRY RUN 模式：检测到止盈信号时只更新DB，不调用 sell_position。"""
        import main
        from main import monitor_positions

        bet = _make_bet()
        mock_bets.return_value = [bet]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": True,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {"price": 0.7}}
        mock_tracker.check_profit_take_exits.return_value = [bet]
        mock_tracker.check_stop_loss_exits.return_value = []

        mock_exec = MagicMock()

        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            monitor_positions()
            # 止盈应调用 close
            mock_tracker.close_position_profit_take.assert_called_once_with(bet)
            # DRY RUN 不调用 sell
            mock_exec.sell_position.assert_not_called()
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_live_mode_sell_called_on_profit_take(
        self, mock_get, mock_bets, mock_comp, _mock_dry
    ):
        """实盘模式：止盈触发时调用 sell_position。"""
        import main
        from main import monitor_positions

        bet = _make_bet()
        mock_bets.return_value = [bet]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sweep_min_shares": 0.01,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {"price": 0.7}}
        mock_tracker.check_profit_take_exits.return_value = [bet]
        mock_tracker.check_stop_loss_exits.return_value = []

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=True, actual_price=0.70, remaining_shares=0.0
        )

        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            with patch("main._verify_chain_position_cleared", return_value=True):
                monitor_positions()
            mock_exec.sell_position.assert_called_once_with(bet)
            mock_tracker.close_position_profit_take.assert_called_once_with(bet)
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_live_mode_sell_called_on_stop_loss(
        self, mock_get, mock_bets, mock_comp, _mock_dry
    ):
        """实盘模式：止损触发时调用 sell_position。"""
        import main
        from main import monitor_positions

        bet = _make_bet(bet_id="b2")
        # First call returns bets, second call (after profit-take) also returns bets
        mock_bets.side_effect = [[bet], [bet]]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sweep_min_shares": 0.01,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {"price": 0.3}}
        mock_tracker.check_profit_take_exits.return_value = []
        mock_tracker.check_stop_loss_exits.return_value = [bet]

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=True, actual_price=0.30, remaining_shares=0.0
        )

        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            with patch("main._verify_chain_position_cleared", return_value=True):
                monitor_positions()
            mock_exec.sell_position.assert_called_once_with(bet)
            mock_tracker.close_position_stop_loss.assert_called_once_with(bet)
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_sell_failure_skips_db_update(
        self, mock_get, mock_bets, mock_comp, _mock_dry
    ):
        """卖出失败时不更新DB，保持仓位OPEN以便下次重试。"""
        import main
        from main import monitor_positions

        bet = _make_bet()
        mock_bets.return_value = [bet]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {}}
        mock_tracker.check_profit_take_exits.return_value = [bet]
        mock_tracker.check_stop_loss_exits.return_value = []

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=False, error="CLOB down"
        )

        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            monitor_positions()
            # 卖出失败 → 不应标记平仓（保持OPEN下次重试）
            mock_tracker.close_position_profit_take.assert_not_called()
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_stop_loss_sell_failure_skips_db_update(
        self, mock_get, mock_bets, mock_comp, _mock_dry
    ):
        """止损卖出失败时不更新DB，保持仓位OPEN以便下次重试。"""
        import main
        from main import monitor_positions

        bet = _make_bet(bet_id="b3")
        mock_bets.side_effect = [[bet], [bet]]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {"price": 0.3}}
        mock_tracker.check_profit_take_exits.return_value = []
        mock_tracker.check_stop_loss_exits.return_value = [bet]

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=False, error="not enough balance / allowance"
        )

        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            monitor_positions()
            # 卖出失败 → 不应标记止损（保持OPEN下次重试）
            mock_tracker.close_position_stop_loss.assert_not_called()
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_exception_in_profit_take_does_not_break_stop_loss(
        self, mock_get, mock_bets, mock_comp
    ):
        """止盈检查异常时，止损检查仍应执行。"""
        import main
        from main import monitor_positions

        bet = _make_bet()
        mock_bets.side_effect = [[bet], [bet]]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": True,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {}}
        mock_tracker.check_profit_take_exits.side_effect = Exception("API timeout")
        mock_tracker.check_stop_loss_exits.return_value = []

        old_tracker = main._outcome_tracker
        try:
            main._outcome_tracker = mock_tracker
            monitor_positions()
            # 止损检查应被调用（即使止盈异常）
            mock_tracker.check_stop_loss_exits.assert_called_once()
        finally:
            main._outcome_tracker = old_tracker

    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.close_paper_bet_manual_exit")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_zombie_position_auto_closed_on_profit_take(
        self, mock_get, mock_bets, mock_manual_exit, mock_comp, _mock_dry
    ):
        """止盈卖出时链上余额为0 → 自动关闭僵尸仓位（不再无限重试）。"""
        import main
        from main import monitor_positions

        bet = _make_bet(bet_id="zombie-1", price=0.10, size=1.10)
        bet["exit_price"] = 0.05
        mock_bets.return_value = [bet]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {"price": 0.05}}
        mock_tracker.check_profit_take_exits.return_value = [bet]
        mock_tracker.check_stop_loss_exits.return_value = []

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=False,
            error="链上 token 余额为 0，无法卖出 [mkt_001/YES] token_id=abc...",
        )

        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            monitor_positions()
            # 僵尸仓位应通过 manual_exit 关闭
            mock_manual_exit.assert_called_once()
            call_kwargs = mock_manual_exit.call_args
            assert call_kwargs[1]["bet_id"] == "zombie-1"
            # 不应调用正常的 close_position_profit_take（因为 sell 失败）
            mock_tracker.close_position_profit_take.assert_not_called()
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.close_paper_bet_manual_exit")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_zombie_position_auto_closed_on_stop_loss(
        self, mock_get, mock_bets, mock_manual_exit, mock_comp, _mock_dry
    ):
        """止损卖出时链上余额为0 → 自动关闭僵尸仓位。"""
        import main
        from main import monitor_positions

        bet = _make_bet(bet_id="zombie-2", price=0.50, size=2.0)
        bet["exit_price"] = 0.30
        mock_bets.side_effect = [[bet], [bet]]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {"price": 0.3}}
        mock_tracker.check_profit_take_exits.return_value = []
        mock_tracker.check_stop_loss_exits.return_value = [bet]

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=False,
            error="链上 token 余额为 0，无法卖出 [mkt_001/YES]",
        )

        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            monitor_positions()
            mock_manual_exit.assert_called_once()
            assert mock_manual_exit.call_args[1]["bet_id"] == "zombie-2"
            mock_tracker.close_position_stop_loss.assert_not_called()
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_sell_success_uses_actual_price(
        self, mock_get, mock_bets, mock_comp, _mock_dry
    ):
        """卖出成功时使用实际成交价更新 exit_price。"""
        import main
        from main import monitor_positions

        bet = _make_bet(bet_id="b-actual", price=0.10, size=1.10)
        bet["exit_price"] = 0.90
        mock_bets.return_value = [bet]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sweep_min_shares": 0.01,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {"price": 0.9}}
        mock_tracker.check_profit_take_exits.return_value = [bet]
        mock_tracker.check_stop_loss_exits.return_value = []

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=True,
            actual_price=0.88,
            filled_size=10.0,
            tx_hash="0xabc",
            remaining_shares=0.0,
        )

        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            with patch("main._verify_chain_position_cleared", return_value=True):
                monitor_positions()
            # close_position_profit_take 应被调用，并且 exit_price 已更新
            mock_tracker.close_position_profit_take.assert_called_once()
            passed_bet = mock_tracker.close_position_profit_take.call_args[0][0]
            assert passed_bet["exit_price"] == 0.88
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_individual_bet_exception_does_not_block_others(
        self, mock_get, mock_bets, mock_comp
    ):
        """单个仓位处理异常不影响其他仓位的止盈/止损。"""
        import main
        from main import monitor_positions

        bet1 = _make_bet(bet_id="good-1")
        bet2 = _make_bet(bet_id="bad-1")
        bet3 = _make_bet(bet_id="good-2")
        mock_bets.return_value = [bet1, bet2, bet3]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": True,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {}}
        mock_tracker.check_profit_take_exits.return_value = [bet1, bet2, bet3]
        mock_tracker.check_stop_loss_exits.return_value = []

        # bet2 处理时抛异常
        call_count = [0]

        def close_side_effect(b):
            call_count[0] += 1
            if b["bet_id"] == "bad-1":
                raise Exception("DB error")

        mock_tracker.close_position_profit_take.side_effect = close_side_effect

        old_tracker = main._outcome_tracker
        try:
            main._outcome_tracker = mock_tracker
            monitor_positions()
            # 应该尝试3次 close（bet1成功, bet2异常, bet3成功）
            assert mock_tracker.close_position_profit_take.call_count == 3
        finally:
            main._outcome_tracker = old_tracker


class TestPartialSellOrphanBet:
    """部分卖出（孤儿单修复）相关测试。"""

    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.reduce_paper_bet_partial_sell")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_partial_sell_reduces_db_on_profit_take(
        self, mock_get, mock_bets, mock_reduce, mock_comp, _mock_dry
    ):
        """止盈部分卖出 → 调用 reduce_paper_bet_partial_sell 缩减仓位。"""
        import main
        from main import monitor_positions

        bet = _make_bet(bet_id="ps-1", price=0.40, size=4.0)
        bet["exit_price"] = 0.80
        mock_bets.return_value = [bet]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sweep_min_shares": 0.01,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {"price": 0.8}}
        mock_tracker.check_profit_take_exits.return_value = [bet]
        mock_tracker.check_stop_loss_exits.return_value = []

        # sell_position 返回 remaining_shares > sweep_min → 部分卖出
        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=True,
            actual_price=0.78,
            filled_size=3.50,
            remaining_shares=0.5,
            tx_hash="0xpartial",
        )

        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            monitor_positions()
            # 应调用 reduce（部分卖出），不应调用 close（完全关闭）
            mock_reduce.assert_called_once()
            args = mock_reduce.call_args[1]
            assert args["bet_id"] == "ps-1"
            assert args["sold_usdc"] == 3.50
            # remaining_usdc = 0.5 shares × 0.40 entry = 0.20
            assert args["remaining_usdc"] == 0.20
            mock_tracker.close_position_profit_take.assert_not_called()
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.reduce_paper_bet_partial_sell")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_partial_sell_reduces_db_on_stop_loss(
        self, mock_get, mock_bets, mock_reduce, mock_comp, _mock_dry
    ):
        """止损部分卖出 → 调用 reduce_paper_bet_partial_sell 缩减仓位。"""
        import main
        from main import monitor_positions

        bet = _make_bet(bet_id="ps-2", price=0.50, size=5.0)
        bet["exit_price"] = 0.30
        # 止损需要通过两次 mock_bets 调用
        mock_bets.side_effect = [[bet], [bet]]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sweep_min_shares": 0.01,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {"price": 0.3}}
        mock_tracker.check_profit_take_exits.return_value = []
        mock_tracker.check_stop_loss_exits.return_value = [bet]

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=True,
            actual_price=0.28,
            filled_size=2.50,
            remaining_shares=0.8,
            tx_hash="0xstop_partial",
        )

        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            monitor_positions()
            mock_reduce.assert_called_once()
            args = mock_reduce.call_args[1]
            assert args["bet_id"] == "ps-2"
            assert args["sold_usdc"] == 2.50
            # remaining_usdc = 0.8 shares × 0.50 entry = 0.40
            assert args["remaining_usdc"] == 0.40
            mock_tracker.close_position_stop_loss.assert_not_called()
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.reduce_paper_bet_partial_sell")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_zero_remaining_shares_closes_normally(
        self, mock_get, mock_bets, mock_reduce, mock_comp, _mock_dry
    ):
        """remaining_shares=0 → 正常关闭，不触发 partial sell。"""
        import main
        from main import monitor_positions

        bet = _make_bet(bet_id="full-1", price=0.40, size=4.0)
        bet["exit_price"] = 0.80
        mock_bets.return_value = [bet]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sweep_min_shares": 0.01,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {"price": 0.8}}
        mock_tracker.check_profit_take_exits.return_value = [bet]
        mock_tracker.check_stop_loss_exits.return_value = []

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=True,
            actual_price=0.79,
            filled_size=8.0,
            remaining_shares=0.0,
            tx_hash="0xfull",
        )

        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            with patch("main._verify_chain_position_cleared", return_value=True):
                monitor_positions()
            mock_reduce.assert_not_called()
            mock_tracker.close_position_profit_take.assert_called_once()
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.reduce_paper_bet_partial_sell")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_tiny_residual_below_sweep_min_closes_normally(
        self, mock_get, mock_bets, mock_reduce, mock_comp, _mock_dry
    ):
        """残余 < sweep_min_shares → 视为全额卖出，正常关闭。"""
        import main
        from main import monitor_positions

        bet = _make_bet(bet_id="tiny-1", price=0.40, size=4.0)
        bet["exit_price"] = 0.80
        mock_bets.return_value = [bet]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sweep_min_shares": 0.05,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {"mkt_001": {"price": 0.8}}
        mock_tracker.check_profit_take_exits.return_value = [bet]
        mock_tracker.check_stop_loss_exits.return_value = []

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=True,
            actual_price=0.79,
            filled_size=7.90,
            remaining_shares=0.03,  # < 0.05 sweep_min
            tx_hash="0xtiny",
        )

        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            with patch("main._verify_chain_position_cleared", return_value=True):
                monitor_positions()
            mock_reduce.assert_not_called()
            mock_tracker.close_position_profit_take.assert_called_once()
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    def test_handle_partial_sell_pnl_calculation(self):
        """_handle_partial_sell 的 PnL 计算逻辑正确性。"""
        from main import _handle_partial_sell

        bet = _make_bet(bet_id="calc-1", price=0.50, size=5.0)
        sell_result = MagicMock(
            actual_price=0.70,
            filled_size=3.50,  # proceeds from sold portion
            remaining_shares=3.0,  # 3 shares still held
        )

        with patch("main.reduce_paper_bet_partial_sell") as mock_reduce:
            _handle_partial_sell(bet, sell_result, "profit_take")
            mock_reduce.assert_called_once()
            args = mock_reduce.call_args[1]

            # remaining_usdc = 3.0 shares × 0.50 entry = 1.50
            assert args["remaining_usdc"] == 1.50
            # cost of sold portion = size - remaining_usdc = 5.0 - 1.50 = 3.50
            # sold_pnl = proceeds - cost_of_sold = 3.50 - 3.50 = 0.0
            assert args["sold_pnl"] == 0.0
            assert args["sold_usdc"] == 3.50
            assert args["bet_id"] == "calc-1"

    def test_handle_partial_sell_profitable_exit(self):
        """_handle_partial_sell 盈利场景。"""
        from main import _handle_partial_sell

        bet = _make_bet(bet_id="calc-2", price=0.40, size=4.0)
        # sold 8 shares (of 10) at 0.80 → proceeds = 6.40; remaining = 2 shares
        sell_result = MagicMock(
            actual_price=0.80,
            filled_size=6.40,
            remaining_shares=2.0,
        )

        with patch("main.reduce_paper_bet_partial_sell") as mock_reduce:
            _handle_partial_sell(bet, sell_result, "profit_take")
            args = mock_reduce.call_args[1]

            # remaining_usdc = 2.0 × 0.40 = 0.80
            assert args["remaining_usdc"] == 0.80
            # cost of sold = 4.0 - 0.80 = 3.20
            # sold_pnl = 6.40 - 3.20 = 3.20
            assert args["sold_pnl"] == 3.20
            assert args["sold_usdc"] == 6.40


class TestWriteOffMechanism:
    """_force_write_off_position 与 _sell_failure_counts 重试计数器测试。"""

    def _setup_live_mocks(self, mock_get, bet, profit_take=True, stop_loss=False):
        """复用：将 bet 注入 outcome_tracker，配置实盘模式。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sweep_min_shares": 0.01,
            "executor.max_sell_retries_before_writeoff": 3,
        }.get(key, default)
        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {
            bet["market_id"]: {"price": bet.get("exit_price", 0.1)}
        }
        mock_tracker.check_profit_take_exits.return_value = [bet] if profit_take else []
        mock_tracker.check_stop_loss_exits.return_value = [bet] if stop_loss else []
        return mock_tracker

    # ── 低于最小下单量 ────────────────────────────────────────────────────────

    @patch("main.requests.get")
    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.close_paper_bet_profit_take")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_below_min_size_profit_take_immediate_writeoff(
        self,
        mock_get,
        mock_bets,
        mock_manual_exit,
        mock_comp,
        _mock_dry,
        mock_requests_get,
    ):
        """止盈时低于 Polymarket 最小下单量（5 shares）→ 立即强制核销，不计入重试。
        SUPREME 1 修复: 仅在市场 resolved=True 时允许核销。
        """
        import main
        from main import monitor_positions

        bet = _make_bet(bet_id="tiny-1", price=0.50, size=1.5)
        bet["exit_price"] = 0.70
        mock_bets.return_value = [bet]
        mock_tracker = self._setup_live_mocks(mock_get, bet, profit_take=True)

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=False,
            error=(
                "PolyApiException[status_code=400, error_message={'error': "
                "'order 0xabc is invalid. Size (3.14) lower than the minimum: 5'}]"
            ),
        )

        # Mock Gamma API: market resolved → writeoff allowed
        mock_gamma_resp = MagicMock()
        mock_gamma_resp.json.return_value = {"resolved": True}
        mock_gamma_resp.raise_for_status = MagicMock()
        mock_requests_get.return_value = mock_gamma_resp

        # 重置失败计数
        main._sell_failure_counts.clear()
        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            monitor_positions()
            # 市场 resolved=True → 应核销
            mock_manual_exit.assert_called_once()
            assert mock_manual_exit.call_args[1]["bet_id"] == "tiny-1"
            # 正常关闭路径不应被调用
            mock_tracker.close_position_profit_take.assert_not_called()
            # 失败计数器应保持清空（核销后弹出）
            assert "tiny-1" not in main._sell_failure_counts
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    @patch("main.requests.get")
    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.close_paper_bet_stop_loss")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_below_min_size_stop_loss_immediate_writeoff(
        self,
        mock_get,
        mock_bets,
        mock_manual_exit,
        mock_comp,
        _mock_dry,
        mock_requests_get,
    ):
        """止损时低于最小下单量 + 市场已resolved → 立即强制核销。"""
        import main
        from main import monitor_positions

        bet = _make_bet(bet_id="tiny-sl-1", price=0.50, size=1.5)
        bet["exit_price"] = 0.25
        mock_bets.side_effect = [[bet], [bet]]
        mock_tracker = self._setup_live_mocks(
            mock_get, bet, profit_take=False, stop_loss=True
        )

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=False,
            error="order abc is invalid. Size (2.8) lower than the minimum: 5",
        )

        # Mock Gamma API: market resolved → writeoff allowed
        mock_gamma_resp = MagicMock()
        mock_gamma_resp.json.return_value = {"resolved": True}
        mock_gamma_resp.raise_for_status = MagicMock()
        mock_requests_get.return_value = mock_gamma_resp

        main._sell_failure_counts.clear()
        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            monitor_positions()
            mock_manual_exit.assert_called_once()
            assert mock_manual_exit.call_args[1]["bet_id"] == "tiny-sl-1"
            mock_tracker.close_position_stop_loss.assert_not_called()
            assert "tiny-sl-1" not in main._sell_failure_counts
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor

    # ── 重试计数器 ────────────────────────────────────────────────────────────

    @patch("main.is_dry_run", return_value=False)
    @patch("main._get_components")
    @patch("main.close_paper_bet_manual_exit")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_timeout_increments_failure_counter(
        self, mock_get, mock_bets, mock_manual_exit, mock_comp, _mock_dry
    ):
        """首次超时只增加计数器，不核销仓位。"""
        import main
        from main import monitor_positions

        bid = "timeout-1"
        bet = _make_bet(bet_id=bid, price=0.12, size=1.5)
        bet["exit_price"] = 0.06
        mock_bets.return_value = [bet]
        mock_tracker = self._setup_live_mocks(mock_get, bet, profit_take=True)

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=False, error="卖出未成交: TIMEOUT_CANCELLED"
        )

        main._sell_failure_counts.clear()
        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            monitor_positions()
            # 首次超时：只累加计数，不核销
            assert main._sell_failure_counts.get(bid, 0) == 1
            mock_manual_exit.assert_not_called()
            mock_tracker.close_position_profit_take.assert_not_called()
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor
            main._sell_failure_counts.clear()

    @patch("main.is_dry_run", return_value=False)
    @patch("main.increment_sell_fail_count")
    @patch("main._get_components")
    @patch("main.close_paper_bet_write_off")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_timeout_keeps_open_no_writeoff(
        self,
        mock_get,
        mock_bets,
        mock_manual_exit,
        mock_comp,
        _mock_incr,
        _mock_dry,
    ):
        """超时累计无论多少次，都保持OPEN继续重试，永不核销。"""
        import main
        from main import monitor_positions

        bid = "timeout-2"
        bet = _make_bet(bet_id=bid, price=0.12, size=1.5)
        bet["exit_price"] = 0.06
        mock_bets.return_value = [bet]
        mock_tracker = self._setup_live_mocks(mock_get, bet, profit_take=True)

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=False, error="卖出未成交: TIMEOUT_CANCELLED"
        )

        # 预填充计数：已失败 99 次，本次是第 100 次
        main._sell_failure_counts[bid] = 99
        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            monitor_positions()
            # 第 100 次超时 → 仍保持OPEN，不核销
            mock_manual_exit.assert_not_called()
            assert main._sell_failure_counts[bid] == 100
            mock_tracker.close_position_profit_take.assert_not_called()
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor
            main._sell_failure_counts.clear()

    @patch("main.is_dry_run", return_value=False)
    @patch("main.increment_sell_fail_count")
    @patch("main._get_components")
    @patch("main.close_paper_bet_write_off")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    def test_timeout_stop_loss_keeps_open_no_writeoff(
        self,
        mock_get,
        mock_bets,
        mock_manual_exit,
        mock_comp,
        _mock_incr,
        _mock_dry,
    ):
        """止损超时无论多少次，都保持OPEN继续重试，永不核销。"""
        import main
        from main import monitor_positions

        bid = "sl-timeout-1"
        bet = _make_bet(bet_id=bid, price=0.50, size=2.0)
        bet["exit_price"] = 0.25
        mock_bets.side_effect = [[bet], [bet]]
        mock_tracker = self._setup_live_mocks(
            mock_get, bet, profit_take=False, stop_loss=True
        )

        mock_exec = MagicMock()
        mock_exec.sell_position.return_value = MagicMock(
            success=False, error="卖出未成交: TIMEOUT_CANCELLED"
        )

        main._sell_failure_counts[bid] = 99  # 已失败 99 次
        old_tracker = main._outcome_tracker
        old_executor = main._executor
        try:
            main._outcome_tracker = mock_tracker
            main._executor = mock_exec
            monitor_positions()
            # 第 100 次超时 → 仍保持OPEN，不核销
            mock_manual_exit.assert_not_called()
            assert main._sell_failure_counts[bid] == 100
            mock_tracker.close_position_stop_loss.assert_not_called()
        finally:
            main._outcome_tracker = old_tracker
            main._executor = old_executor
            main._sell_failure_counts.clear()

    # ── 核销 PnL 计算验证 ─────────────────────────────────────────────────────

    def test_force_write_off_pnl_calculation(self):
        """_force_write_off_position 的 PnL 计算正确（亏损场景）。"""
        from main import _force_write_off_position

        bet = _make_bet(bet_id="wo-1", price=0.12, size=1.44)  # 12 shares @ 0.12
        bet["exit_price"] = 0.06  # 当前市价

        # T55: stop_loss_write_off trigger → close_paper_bet_stop_loss
        with patch("main.close_paper_bet_stop_loss") as mock_exit:
            _force_write_off_position(bet, "stop_loss_write_off", reason="test")
            mock_exit.assert_called_once()
            kwargs = mock_exit.call_args[1]
            assert kwargs["bet_id"] == "wo-1"
            # shares = 1.44 / 0.12 = 12; payout = 12 × 0.06 = 0.72
            assert abs(kwargs["payout"] - 0.72) < 0.001
            # pnl = 0.72 - 1.44 = -0.72
            assert abs(kwargs["pnl"] - (-0.72)) < 0.001

    def test_force_write_off_zero_exit_price(self):
        """exit_price 为 0 时核销 payout = 0（市场已清零）。"""
        from main import _force_write_off_position

        bet = _make_bet(bet_id="wo-zero", price=0.50, size=2.0)
        bet["exit_price"] = 0.0

        # T55: stop_loss_write_off trigger → close_paper_bet_stop_loss
        with patch("main.close_paper_bet_stop_loss") as mock_exit:
            _force_write_off_position(bet, "stop_loss_write_off", reason="market_dead")
            kwargs = mock_exit.call_args[1]
            assert kwargs["payout"] == 0.0
            assert kwargs["pnl"] == -2.0  # 全损

    def test_force_write_off_db_exception_logged(self):
        """close DB 函数抛异常时 _force_write_off_position 不传播异常。"""
        from main import _force_write_off_position

        bet = _make_bet(bet_id="wo-err", price=0.50, size=1.0)
        bet["exit_price"] = 0.2

        # T55: trigger="test" → close_paper_bet_stop_loss
        with patch(
            "main.close_paper_bet_stop_loss", side_effect=Exception("DB locked")
        ):
            # 不应抛异常（内部 catch + log.error）
            _force_write_off_position(bet, "test", reason="db_error_test")


# ---------------------------------------------------------------------------
# Phase 42.1: 主动小仓位扫描（proactive_min_shares_scan）测试
# ---------------------------------------------------------------------------


class TestProactiveSmallPositionScan:
    """验证 monitor_positions() 开头的主动小仓位扫描逻辑。"""

    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    @patch("main._force_write_off_position")
    def test_small_position_auto_writeoff(
        self, mock_writeoff, mock_get, mock_bets, mock_comp
    ):
        """Phase 55: DRY_RUN 模式下 matched_shares < min_order_shares 的仓位
        不再被核销，而是留在 remaining_bets 中等待市场自然结算。"""
        import main
        from main import monitor_positions

        small_bet = _make_bet(bet_id="small-1", price=0.50, size=1.50)
        small_bet["matched_shares"] = 3.0  # < 5 shares
        normal_bet = _make_bet(bet_id="normal-1", price=0.50, size=5.0)
        normal_bet["matched_shares"] = 10.0  # >= 5 shares

        mock_bets.return_value = [small_bet, normal_bet]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": True,
            "executor.min_order_shares": 5,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {
            "mkt_001": {"bestBid": "0.70"}
        }
        mock_tracker.check_profit_take_exits.return_value = []
        mock_tracker.check_stop_loss_exits.return_value = []

        old_tracker = main._outcome_tracker
        try:
            main._outcome_tracker = mock_tracker
            monitor_positions()
            # Phase 55: DRY_RUN 模式小仓位不核销（等待市场自然结算）
            mock_writeoff.assert_not_called()
            # 两个仓位均进入止盈/止损检查（small_bet 也被保留）
            pt_bets = mock_tracker.check_profit_take_exits.call_args[0][0]
            sl_bets = mock_tracker.check_stop_loss_exits.call_args[0][0]
            bet_ids_pt = {b["bet_id"] for b in pt_bets}
            bet_ids_sl = {b["bet_id"] for b in sl_bets}
            assert "small-1" in bet_ids_pt
            assert "normal-1" in bet_ids_pt
            assert bet_ids_pt == bet_ids_sl
        finally:
            main._outcome_tracker = old_tracker

    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    @patch("main._force_write_off_position")
    def test_normal_position_not_writeoff(
        self, mock_writeoff, mock_get, mock_bets, mock_comp
    ):
        """matched_shares >= min_order_shares 的仓位不应被核销。"""
        import main
        from main import monitor_positions

        bet = _make_bet(bet_id="ok-1", price=0.50, size=3.0)
        bet["matched_shares"] = 6.0  # >= 5 shares

        mock_bets.return_value = [bet]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": True,
            "executor.min_order_shares": 5,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {
            "mkt_001": {"bestBid": "0.70"}
        }
        mock_tracker.check_profit_take_exits.return_value = []
        mock_tracker.check_stop_loss_exits.return_value = []

        old_tracker = main._outcome_tracker
        try:
            main._outcome_tracker = mock_tracker
            monitor_positions()
            mock_writeoff.assert_not_called()
        finally:
            main._outcome_tracker = old_tracker

    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    @patch("main._force_write_off_position")
    def test_small_position_uses_market_price_for_exit(
        self, mock_writeoff, mock_get, mock_bets, mock_comp
    ):
        """Phase 55: DRY_RUN 模式下小仓位不核销，止盈/止损检查正常运行。"""
        import main
        from main import monitor_positions

        small_bet = _make_bet(bet_id="exit-price-1", price=0.40, size=1.0)
        small_bet["matched_shares"] = 2.5
        small_bet["outcome"] = "YES"

        mock_bets.return_value = [small_bet]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": True,
            "executor.min_order_shares": 5,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {
            "mkt_001": {"bestBid": "0.85", "bestAsk": "0.87"}
        }
        mock_tracker.check_profit_take_exits.return_value = []
        mock_tracker.check_stop_loss_exits.return_value = []

        old_tracker = main._outcome_tracker
        try:
            main._outcome_tracker = mock_tracker
            monitor_positions()
            # Phase 55: DRY_RUN 模式不核销，等待自然结算
            mock_writeoff.assert_not_called()
            # small_bet 应进入正常止盈/止损流程
            pt_bets = mock_tracker.check_profit_take_exits.call_args[0][0]
            assert any(b["bet_id"] == "exit-price-1" for b in pt_bets)
        finally:
            main._outcome_tracker = old_tracker

    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    @patch("main._force_write_off_position")
    def test_derived_shares_fallback(
        self, mock_writeoff, mock_get, mock_bets, mock_comp
    ):
        """matched_shares=0 时回退到 size_usdc/market_price 计算 shares。
        Phase 55: DRY_RUN 模式下即使推算 shares 不足，也不核销，等待自然结算。"""
        import main
        from main import monitor_positions

        bet = _make_bet(bet_id="derived-1", price=0.50, size=2.0)
        bet["matched_shares"] = 0  # No matched_shares recorded
        # derived = 2.0 / 0.50 = 4.0 < 5 → DRY_RUN 模式: 不核销，继续监控

        mock_bets.return_value = [bet]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": True,
            "executor.min_order_shares": 5,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {
            "mkt_001": {"bestBid": "0.60"}
        }
        mock_tracker.check_profit_take_exits.return_value = []
        mock_tracker.check_stop_loss_exits.return_value = []

        old_tracker = main._outcome_tracker
        try:
            main._outcome_tracker = mock_tracker
            monitor_positions()
            # Phase 55: DRY_RUN 不核销（等待市场自然结算）
            mock_writeoff.assert_not_called()
            # derived bet 仍进入止盈/止损检查
            pt_bets = mock_tracker.check_profit_take_exits.call_args[0][0]
            assert any(b["bet_id"] == "derived-1" for b in pt_bets)
        finally:
            main._outcome_tracker = old_tracker

    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    @patch("main._force_write_off_position")
    def test_multiple_small_positions_all_writeoff(
        self, mock_writeoff, mock_get, mock_bets, mock_comp
    ):
        """Phase 55: DRY_RUN 模式下多个小仓位不核销，全部进入止盈/止损监控。"""
        import main
        from main import monitor_positions

        s1 = _make_bet(bet_id="s1", market_id="m1", price=0.3, size=0.9)
        s1["matched_shares"] = 3.0
        s2 = _make_bet(bet_id="s2", market_id="m2", price=0.4, size=1.2)
        s2["matched_shares"] = 3.0
        ok = _make_bet(bet_id="ok", market_id="m3", price=0.5, size=5.0)
        ok["matched_shares"] = 10.0

        mock_bets.return_value = [s1, s2, ok]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": True,
            "executor.min_order_shares": 5,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {
            "m1": {"bestBid": "0.50"},
            "m2": {"bestBid": "0.60"},
            "m3": {"bestBid": "0.70"},
        }
        mock_tracker.check_profit_take_exits.return_value = []
        mock_tracker.check_stop_loss_exits.return_value = []

        old_tracker = main._outcome_tracker
        try:
            main._outcome_tracker = mock_tracker
            monitor_positions()
            # Phase 55: 全部不核销，等待自然结算
            mock_writeoff.assert_not_called()
            # 三个仓位（含小仓位）均进入止盈/止损检查
            pt_bets = mock_tracker.check_profit_take_exits.call_args[0][0]
            pt_ids = {b["bet_id"] for b in pt_bets}
            assert pt_ids == {"s1", "s2", "ok"}
        finally:
            main._outcome_tracker = old_tracker


class TestTopupDisabled:
    """T210追加：验证补买逻辑已永久禁用，两个函数始终返回 False。"""

    def test_topup_only_always_returns_false(self):
        """_topup_only_small_position 始终返回 False（补买已禁用）。"""
        from main import _topup_only_small_position

        bet = _make_bet(bet_id="t1", price=0.5, size=1.0, matched_shares=2.0)
        result = _topup_only_small_position(bet, {"m1": {"bestAsk": 0.50}})
        assert result is False

    def test_smart_topup_always_returns_false(self):
        """_smart_topup_and_sell_for_exit 始终返回 False（补买已禁用）。"""
        from main import _smart_topup_and_sell_for_exit

        bet = _make_bet(bet_id="t2", price=0.5, size=1.0, matched_shares=2.0)
        result = _smart_topup_and_sell_for_exit(bet, {"m1": {}}, "stop_loss")
        assert result is False

    def test_smart_topup_returns_false_for_profit_take(self):
        """_smart_topup_and_sell_for_exit 止盈场景也返回 False。"""
        from main import _smart_topup_and_sell_for_exit

        bet = _make_bet(bet_id="t3", price=0.3, size=0.9, matched_shares=3.0)
        result = _smart_topup_and_sell_for_exit(bet, {"m1": {}}, "profit_take")
        assert result is False

    @patch("main._get_components")
    @patch("main.get_open_paper_bets")
    @patch("main.get")
    @patch("main.is_dry_run", return_value=False)
    @patch("main._force_write_off_position")
    def test_live_mode_small_position_stays_open(
        self, mock_writeoff, mock_dry, mock_get, mock_bets, mock_comp
    ):
        """实盘模式下小仓位保持 OPEN 等待结算（不补买，不核销）。"""
        import main
        from main import monitor_positions

        small = _make_bet(
            bet_id="s1", market_id="m1", price=0.5, size=1.0, matched_shares=2.0
        )
        mock_bets.return_value = [small]
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
        }.get(key, default)

        mock_tracker = MagicMock()
        mock_tracker._fetch_market_statuses.return_value = {
            "m1": {
                "bestAsk": "0.50",
                "bestBid": "0.45",
                "clobTokenIds": ["tok_yes", "tok_no"],
            }
        }
        mock_tracker.check_profit_take_exits.return_value = []
        mock_tracker.check_stop_loss_exits.return_value = []

        old_tracker = main._outcome_tracker
        try:
            main._outcome_tracker = mock_tracker
            monitor_positions()
            # 小仓位不核销，保持 OPEN 进入止盈/止损检查
            mock_writeoff.assert_not_called()
            pt_bets = mock_tracker.check_profit_take_exits.call_args[0][0]
            assert any(b["bet_id"] == "s1" for b in pt_bets)
        finally:
            main._outcome_tracker = old_tracker


# =====================================================================
# _is_market_closed_error & _close_market_resolved_position 测试
# =====================================================================


class TestMarketClosedError:
    """测试 orderbook 不存在（市场已结算）的检测和处理。"""

    def test_is_market_closed_error_orderbook(self):
        """'orderbook does not exist' 应被识别为 market_closed。"""
        from main import _is_market_closed_error

        msg = (
            "PolyApiException[status_code=400, error_message={'error': "
            "'the orderbook 98178294989379785166501833448918305983397007979544287236312450502929339454438 "
            "does not exist'}]"
        )
        assert _is_market_closed_error(msg) is True

    def test_is_market_closed_error_empty(self):
        from main import _is_market_closed_error

        assert _is_market_closed_error("") is False
        assert _is_market_closed_error(None) is False

    def test_is_market_closed_error_timeout(self):
        """TIMEOUT_CANCELLED 不应被识别为 market_closed。"""
        from main import _is_market_closed_error

        assert _is_market_closed_error("订单未成交: TIMEOUT_CANCELLED") is False

    def test_is_market_closed_error_balance_zero(self):
        """token 余额为 0 不应被识别为 market_closed。"""
        from main import _is_market_closed_error

        assert _is_market_closed_error("链上 token 余额为 0，无法卖出") is False

    def test_is_market_closed_error_min_order_size(self):
        """below_min_order_size 不应被识别为 market_closed。"""
        from main import _is_market_closed_error

        assert _is_market_closed_error("below_min_order_size: sell_size=3.00") is False

    @patch("main.close_paper_bet_manual_exit")
    def test_close_market_resolved_position(self, mock_close):
        """market resolved 时应以 manual_exit 方式关闭仓位。"""
        from main import _close_market_resolved_position

        bet = {
            "bet_id": "abc12345-dead-beef",
            "market_price": 0.5,
            "exit_price": 0.8,
            "size_usdc": 2.0,
        }
        _close_market_resolved_position(bet, "profit_take_market_resolved")
        mock_close.assert_called_once()
        call_kwargs = mock_close.call_args[1]
        assert call_kwargs["bet_id"] == "abc12345-dead-beef"
        assert call_kwargs["exit_price"] == 0.8
        # PnL = shares(4.0) * exit(0.8) - size(2.0) = 1.2
        assert call_kwargs["pnl"] == 1.2

    @patch("main._close_market_resolved_position")
    @patch("main._executor")
    @patch("main.get")
    def test_topup_disabled_does_not_detect_market_closed(
        self, mock_get, mock_exec, mock_close
    ):
        """T210追加：TopUp 已禁用，直接返回 False（不再检测 market_closed）。"""
        from main import _topup_only_small_position

        bet = {
            "bet_id": "test1234",
            "market_id": "mkt_001",
            "outcome": "YES",
            "market_price": 0.5,
            "size_usdc": 1.0,
            "matched_shares": 3.0,
            "question": "Test?",
            "condition_id": "0xabc",
        }
        market_data = {
            "mkt_001": {"bestAsk": 0.5, "clobTokenIds": ["tok_yes", "tok_no"]}
        }
        result = _topup_only_small_position(bet, market_data)
        assert result is False  # 补买已禁用，始终返回 False
        mock_exec.execute.assert_not_called()
        mock_close.assert_not_called()
