"""T55: 投注单卖出链路紧急修复 — 单元测试

验证三个根因修复：
1. _force_write_off_position 根据 trigger 路由到正确的 DB 关闭函数
2. monitor_positions 防止同一周期止盈/止损双重捕获
3. certainty_sell 通过 below_min write-off 正确记录为 profit_take
"""

import os
from unittest.mock import MagicMock, patch

import pytest


# ======================================================================
# Fix 1: _force_write_off_position trigger routing
# ======================================================================
class TestWriteOffTriggerRouting:
    """T55: 验证 _force_write_off_position 根据 trigger 路由到正确的 close 函数。"""

    @patch("main.is_dry_run", return_value=True)
    @patch("main.close_paper_bet_profit_take")
    @patch("main.close_paper_bet_stop_loss")
    def test_profit_take_trigger_calls_profit_take_close(self, mock_sl, mock_pt, _dry):
        """trigger 含 'profit_take' → close_paper_bet_profit_take。"""
        from main import _force_write_off_position

        bet = {
            "bet_id": "t55_pt_001",
            "market_price": 0.50,
            "exit_price": 0.90,
            "size_usdc": 2.00,
        }
        _force_write_off_position(
            bet,
            trigger="profit_take_min_shares",
            reason="below_min_order_size_force_writeoff",
        )
        mock_pt.assert_called_once()
        mock_sl.assert_not_called()

    @patch("main.is_dry_run", return_value=True)
    @patch("main.close_paper_bet_profit_take")
    @patch("main.close_paper_bet_stop_loss")
    def test_certainty_trigger_calls_profit_take_close(self, mock_sl, mock_pt, _dry):
        """trigger 含 'certainty' → close_paper_bet_profit_take。"""
        from main import _force_write_off_position

        bet = {
            "bet_id": "t55_cert_001",
            "market_price": 0.50,
            "exit_price": 0.95,
            "size_usdc": 2.00,
        }
        _force_write_off_position(
            bet, trigger="certainty_sell_writeoff", reason="below_min"
        )
        mock_pt.assert_called_once()
        mock_sl.assert_not_called()

    @patch("main.is_dry_run", return_value=True)
    @patch("main.close_paper_bet_profit_take")
    @patch("main.close_paper_bet_stop_loss")
    def test_stop_loss_trigger_calls_stop_loss_close(self, mock_sl, mock_pt, _dry):
        """trigger 含 'stop_loss' → close_paper_bet_stop_loss。"""
        from main import _force_write_off_position

        bet = {
            "bet_id": "t55_sl_001",
            "market_price": 0.50,
            "exit_price": 0.20,
            "size_usdc": 2.00,
        }
        _force_write_off_position(
            bet,
            trigger="stop_loss_min_shares",
            reason="below_min_order_size_force_writeoff",
        )
        mock_sl.assert_called_once()
        mock_pt.assert_not_called()

    @patch("main.is_dry_run", return_value=True)
    @patch("main.close_paper_bet_profit_take")
    @patch("main.close_paper_bet_stop_loss")
    def test_unknown_trigger_defaults_to_stop_loss(self, mock_sl, mock_pt, _dry):
        """trigger 不含 profit_take/certainty → fallback stop_loss。"""
        from main import _force_write_off_position

        bet = {
            "bet_id": "t55_unk_001",
            "market_price": 0.50,
            "exit_price": 0.10,
            "size_usdc": 2.00,
        }
        _force_write_off_position(bet, trigger="timeout_max_retries")
        mock_sl.assert_called_once()
        mock_pt.assert_not_called()

    @patch("main.is_dry_run", return_value=True)
    @patch("main.close_paper_bet_profit_take")
    @patch("main.close_paper_bet_stop_loss")
    def test_below_min_order_size_trigger_routes_to_stop_loss(
        self, mock_sl, mock_pt, _dry
    ):
        """trigger='below_min_order_size' (无 profit_take) → stop_loss。"""
        from main import _force_write_off_position

        bet = {
            "bet_id": "t55_bm_001",
            "market_price": 0.50,
            "exit_price": 0.10,
            "size_usdc": 2.00,
        }
        _force_write_off_position(bet, trigger="below_min_order_size")
        mock_sl.assert_called_once()
        mock_pt.assert_not_called()

    @patch("main.is_dry_run", return_value=True)
    @patch("main.close_paper_bet_profit_take")
    def test_write_off_pnl_calculation(self, mock_pt, _dry):
        """验证核销时 PnL 使用 exit_price 正确计算。"""
        from main import _force_write_off_position

        bet = {
            "bet_id": "t55_pnl_001",
            "market_price": 0.50,
            "exit_price": 0.90,
            "size_usdc": 5.00,
        }
        _force_write_off_position(
            bet, trigger="profit_take_min_shares", reason="below_min"
        )
        call_args = mock_pt.call_args
        assert call_args[1]["bet_id"] == "t55_pnl_001"
        assert call_args[1]["exit_price"] == 0.90
        # shares = 5.0 / 0.5 = 10, payout = 10 * 0.9 = 9.0, pnl = 9.0 - 5.0 = 4.0
        assert call_args[1]["pnl"] == 4.0
        assert call_args[1]["payout"] == 9.0


# ======================================================================
# Fix 2: Same-cycle double-processing prevention
# ======================================================================
class TestSameCycleProtection:
    """T55: 验证止盈卖出失败的仓位不会在同一周期被止损段误捕获。"""

    @patch("main.get")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main._get_components")
    def test_profit_take_sell_failure_excluded_from_stop_loss(
        self, mock_comp, mock_open_bets, mock_dry, mock_get
    ):
        """止盈卖出超时失败的仓位应在同周期止损中被排除。"""
        from main import monitor_positions

        # 配置
        mock_get.side_effect = lambda key, *a, **kw: {
            "paper_trading.min_order_shares": 5,
            "executor.sweep_min_shares": 0.01,
        }.get(key, None)

        bet = {
            "bet_id": "t55_cycle_001",
            "market_id": "mkt_001",
            "market_price": 0.50,
            "exit_price": 0.90,
            "size_usdc": 5.0,
            "matched_shares": 10.0,
            "slug": "test-market",
            "condition_id": "0xcond",
            "outcome": "YES",
        }

        # First call returns bet, second call (after profit_take) also returns bet
        mock_open_bets.side_effect = [[bet], [bet]]

        # Mock components
        mock_executor = MagicMock()
        sell_fail_result = MagicMock()
        sell_fail_result.success = False
        sell_fail_result.error = "TIMEOUT_CANCELLED"
        mock_executor.sell_position.return_value = sell_fail_result

        mock_tracker = MagicMock()
        # certainty_sell triggers profit_take
        exit_bet = dict(bet)
        exit_bet["trigger"] = "certainty_sell"
        exit_bet["exit_price"] = 0.95
        mock_tracker.check_profit_take_exits.return_value = [exit_bet]
        # stop_loss would also trigger for same bet (edge case)
        stop_bet = dict(bet)
        stop_bet["trigger"] = "stop_loss"
        stop_bet["exit_price"] = 0.30
        mock_tracker.check_stop_loss_exits.return_value = [stop_bet]

        mock_comp.return_value = (
            MagicMock(),  # scanner
            MagicMock(),  # signal_engine
            mock_executor,
            MagicMock(),  # monitor
            MagicMock(),  # syncer
            mock_tracker,
        )

        # The sell should only be attempted once (profit_take), not twice (stop_loss)
        with patch("main._outcome_tracker", mock_tracker), patch(
            "main._executor", mock_executor
        ), patch("main._sell_failure_counts", {}), patch(
            "main.increment_sell_fail_count"
        ), patch(
            "main.reset_sell_fail_count"
        ):
            monitor_positions()

        # Executor should be called exactly once (profit_take attempt only)
        # Stop loss should NOT attempt sell for the same bet
        pt_sell_calls = mock_executor.sell_position.call_count
        assert (
            pt_sell_calls == 1
        ), f"Expected 1 sell attempt (profit_take only), got {pt_sell_calls}"


# ======================================================================
# Fix 3: certainty_sell + below_min → profit_take (not stop_loss)
# ======================================================================
class TestCertaintySellBelowMinExitType:
    """T55: certainty_sell 触发 below_min_order_size → 应记录为 profit_take。
    SUPREME 1: 核销仅在 market resolved=True 时允许。
    """

    @patch("main.requests.get")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.close_paper_bet_profit_take")
    @patch("main.close_paper_bet_stop_loss")
    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": ""})
    def test_certainty_sell_below_min_records_profit_take(
        self, mock_sl, mock_pt, _dry, mock_requests_get
    ):
        """certainty_sell → 4.8 shares sold → 0.2 remains → below_min → profit_take。"""
        from main import _force_write_off_position

        # Mock Gamma API: resolved=True → writeoff allowed
        mock_gamma_resp = MagicMock()
        mock_gamma_resp.json.return_value = {"resolved": True}
        mock_gamma_resp.raise_for_status = MagicMock()
        mock_requests_get.return_value = mock_gamma_resp

        # Simulates residual 0.2 shares after partial sell
        bet = {
            "bet_id": "t55_cert_bm_001",
            "market_price": 0.50,
            "exit_price": 0.95,
            "size_usdc": 0.10,  # Residual value
            "condition_id": "0xcond_partial",
            "market_id": "mkt_t55_cert",
        }
        _force_write_off_position(
            bet,
            trigger="profit_take_min_shares",
            reason="below_min_order_size_force_writeoff",
        )
        # Should use profit_take (not stop_loss) because trigger contains 'profit_take'
        mock_pt.assert_called_once()
        mock_sl.assert_not_called()
        # Verify exit_type matches the trigger intent
        assert mock_pt.call_args[1]["exit_price"] == 0.95

    @patch("main.requests.get")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.close_paper_bet_profit_take")
    @patch("main.close_paper_bet_stop_loss")
    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": ""})
    def test_stop_loss_below_min_records_stop_loss(
        self, mock_sl, mock_pt, _dry, mock_requests_get
    ):
        """stop_loss → below_min → 应记录为 stop_loss。"""
        from main import _force_write_off_position

        # Mock Gamma API: resolved=True → writeoff allowed
        mock_gamma_resp = MagicMock()
        mock_gamma_resp.json.return_value = {"resolved": True}
        mock_gamma_resp.raise_for_status = MagicMock()
        mock_requests_get.return_value = mock_gamma_resp

        bet = {
            "bet_id": "t55_sl_bm_001",
            "market_price": 0.50,
            "exit_price": 0.20,
            "size_usdc": 0.10,
            "condition_id": "0xcond_sl_partial",
            "market_id": "mkt_t55_sl",
        }
        _force_write_off_position(
            bet,
            trigger="stop_loss_min_shares",
            reason="below_min_order_size_force_writeoff",
        )
        mock_sl.assert_called_once()
        mock_pt.assert_not_called()


# ======================================================================
# Regression: write-off 100 次仍 OPEN（SUPREME 1 要求）
# ======================================================================
class TestWriteOffNeverOnActiveBets:
    """SUPREME 1: 卖出失败100次后仍保持OPEN状态，不触发核销。"""

    @patch("main.is_dry_run", return_value=False)
    @patch("main.close_paper_bet_profit_take")
    @patch("main.close_paper_bet_stop_loss")
    @patch("main.increment_sell_fail_count")
    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet"})
    @patch("main.requests.get")
    def test_chain_has_shares_blocks_writeoff_regardless_of_trigger(
        self, mock_req, mock_inc, mock_sl, mock_pt, _dry
    ):
        """链上仍有 shares 时，无论 trigger 如何，都不核销。"""
        from main import _force_write_off_position

        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"conditionId": "0xcond_active", "size": "8.5"}]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        bet = {
            "bet_id": "t55_active_001",
            "market_price": 0.50,
            "exit_price": 0.90,
            "size_usdc": 5.00,
            "condition_id": "0xcond_active",
        }
        # Even with profit_take trigger, chain check blocks write-off
        _force_write_off_position(
            bet, trigger="profit_take_write_off", reason="timeout"
        )
        mock_pt.assert_not_called()
        mock_sl.assert_not_called()
        mock_inc.assert_called_once_with("t55_active_001")
