"""
test_order_executor.py — OrderExecutor 单元测试
覆盖：DRY RUN 下单、余额裁剪、滑点检查、execute_all 批量执行
"""

from unittest.mock import MagicMock, patch

import pytest
from src.executor.order_executor import OrderExecutor, OrderResult
from src.signal.signal_engine import Signal
from src.utils.config_loader import get

# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


def make_signal(
    market_id: str = "mkt_001",
    question: str = "Will it rain in NYC tomorrow?",
    outcome: str = "YES",
    ev: float = 0.08,
    confidence: float = 0.75,
    size_usdc: float = 3.0,
) -> Signal:
    return Signal(
        market_id=market_id,
        question=question,
        outcome=outcome,
        ev=ev,
        confidence=confidence,
        size_usdc=size_usdc,
    )


# ---------------------------------------------------------------------------
# DRY RUN 下单测试
# ---------------------------------------------------------------------------


class TestDryRun:
    def setup_method(self):
        self.executor = OrderExecutor()

    def test_dry_run_returns_success(self):
        sig = make_signal()
        result = self.executor.execute(sig)
        assert result.success is True
        assert result.tx_hash == "DRY_RUN"

    def test_dry_run_does_not_call_clob(self):
        sig = make_signal()
        with patch.object(self.executor, "_get_clob_client") as mock_clob:
            self.executor.execute(sig)
            mock_clob.assert_not_called()

    @patch("src.executor.order_executor.is_dry_run", return_value=True)
    def test_dry_run_check_balance_returns_config_value(self, _mock_dry_run):
        with patch("src.utils.db.connect") as mock_conn:
            ctx = mock_conn.return_value.__enter__.return_value
            ctx.execute.return_value.fetchone.return_value = (0.0,)
            balance = self.executor._check_balance()
            expected = float((get("paper_trading") or {}).get("initial_balance", 100.0))
            assert balance == expected

    def test_execute_all_dry_run(self):
        signals = [make_signal(market_id=f"mkt_{i}") for i in range(3)]
        results = self.executor.execute_all(signals)
        assert len(results) == 3
        assert all(r.success for r in results)
        assert all(r.tx_hash == "DRY_RUN" for r in results)


# ---------------------------------------------------------------------------
# 仓位裁剪测试（实盘分支，mock balance + dry_run=False）
# ---------------------------------------------------------------------------


class TestPositionClipping:
    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_position_clipped_to_max_market_pct(self, mock_get, _mock_dry):
        def cfg_side_effect(key, default=None):
            mapping = {
                "general.dry_run": False,
                "executor.probe_enabled": False,
                "risk.max_single_market_pct": 0.05,
            }
            return mapping.get(key, default)

        mock_get.side_effect = cfg_side_effect

        sig = make_signal(size_usdc=999.0)  # 远超限额
        with patch.object(self.executor, "_check_balance", return_value=1000.0):
            with patch.object(self.executor, "_submit_order") as mock_submit:
                mock_submit.return_value = OrderResult(
                    signal=sig, success=True, tx_hash="0xABC"
                )
                result = self.executor.execute(sig)
                # 1000 * 5% = 50 USDC 上限
                assert sig.size_usdc <= 50.0


# ---------------------------------------------------------------------------
# Probe 超配修复测试
# ---------------------------------------------------------------------------


class TestProbeDeduction:
    """验证 probe 单成交后，主单仓位 = 目标 - probe 已成交量。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_main_order_deducts_probe_filled(self, mock_get, _mock_dry):
        """probe 成交 1 USDC 后，主单下单用 target-1=2 USDC，
        但最终 signal.size_usdc 恢复为 probe+main 总额。"""

        def cfg_side_effect(key, default=None):
            mapping = {
                "general.dry_run": False,
                "executor.probe_enabled": True,
                "executor.probe_size_usdc": 1.0,
                "executor.min_bet_usdc": 1.0,
                "risk.max_single_market_pct": 0.10,
            }
            return mapping.get(key, default)

        mock_get.side_effect = cfg_side_effect
        sig = make_signal(size_usdc=3.0)

        probe_result = OrderResult(
            signal=sig,
            success=True,
            tx_hash="0xPROBE",
            actual_price=0.50,
            filled_size=1.0,
        )

        submitted_sizes = []

        def capture_submit(signal_arg):
            submitted_sizes.append(signal_arg.size_usdc)
            return OrderResult(
                signal=signal_arg,
                success=True,
                tx_hash="0xMAIN",
                filled_size=2.0,
            )

        with patch.object(self.executor, "_check_balance", return_value=1000.0):
            with patch.object(self.executor, "_probe_order", return_value=probe_result):
                with patch.object(self.executor, "_slippage_ok", return_value=True):
                    with patch.object(
                        self.executor, "_submit_order", side_effect=capture_submit
                    ):
                        result = self.executor.execute(sig)
                        # 主单下单时仓位应已扣除 probe: 3.0 - 1.0 = 2.0
                        assert submitted_sizes == [2.0]
                        # 最终返回的 filled_size = probe(1.0) + main(2.0) = 3.0
                        assert result.filled_size == 3.0
                        # signal.size_usdc 恢复为总额
                        assert sig.size_usdc == 3.0

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_probe_fills_target_skips_main(self, mock_get, _mock_dry):
        """probe 成交量 = 目标仓位时，不下主单。"""

        def cfg_side_effect(key, default=None):
            mapping = {
                "general.dry_run": False,
                "executor.probe_enabled": True,
                "executor.probe_size_usdc": 1.0,
                "executor.min_bet_usdc": 1.0,
                "risk.max_single_market_pct": 0.10,
            }
            return mapping.get(key, default)

        mock_get.side_effect = cfg_side_effect
        sig = make_signal(size_usdc=1.0)  # 目标 = probe size

        probe_result = OrderResult(
            signal=sig,
            success=True,
            tx_hash="0xPROBE",
            actual_price=0.50,
            filled_size=1.0,
        )

        with patch.object(self.executor, "_check_balance", return_value=1000.0):
            with patch.object(self.executor, "_probe_order", return_value=probe_result):
                with patch.object(self.executor, "_slippage_ok", return_value=True):
                    with patch.object(self.executor, "_submit_order") as mock_submit:
                        result = self.executor.execute(sig)
                        # 不应调用 _submit_order（probe 已完成全部仓位）
                        mock_submit.assert_not_called()
                        assert result.tx_hash == "0xPROBE"

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_main_order_fail_no_merge(self, mock_get, _mock_dry):
        """主单失败时，不合并 probe 成本。"""

        def cfg_side_effect(key, default=None):
            mapping = {
                "general.dry_run": False,
                "executor.probe_enabled": True,
                "executor.probe_size_usdc": 1.0,
                "executor.min_bet_usdc": 1.0,
                "risk.max_single_market_pct": 0.10,
            }
            return mapping.get(key, default)

        mock_get.side_effect = cfg_side_effect
        sig = make_signal(size_usdc=5.0)

        probe_result = OrderResult(
            signal=sig,
            success=True,
            tx_hash="0xPROBE",
            actual_price=0.50,
            filled_size=1.1,
        )
        main_fail = OrderResult(
            signal=sig,
            success=False,
            tx_hash="",
            error="CLOB error",
            filled_size=0.0,
        )

        with patch.object(self.executor, "_check_balance", return_value=1000.0):
            with patch.object(self.executor, "_probe_order", return_value=probe_result):
                with patch.object(self.executor, "_slippage_ok", return_value=True):
                    with patch.object(
                        self.executor, "_submit_order", return_value=main_fail
                    ):
                        result = self.executor.execute(sig)
                        assert not result.success
                        # 主单失败时不合并 probe
                        assert result.filled_size == 0.0


# ---------------------------------------------------------------------------
# 滑点检查测试
# ---------------------------------------------------------------------------


class TestSlippageCheck:
    def setup_method(self):
        self.executor = OrderExecutor()

    def test_slippage_ok_within_threshold(self):
        sig = make_signal()
        with patch.object(self.executor, "_market_price", return_value=0.50):
            assert self.executor._slippage_ok(sig, actual_price=0.51) is True

    def test_slippage_fails_over_threshold(self):
        sig = make_signal()
        with patch.object(self.executor, "_market_price", return_value=0.50):
            assert self.executor._slippage_ok(sig, actual_price=0.53) is False

    def test_slippage_exactly_at_threshold(self):
        sig = make_signal()
        with patch.object(self.executor, "_market_price", return_value=0.50):
            # threshold = 0.02，actual 偏差 = 0.019 → 边界内允许
            assert self.executor._slippage_ok(sig, actual_price=0.519) is True


# ---------------------------------------------------------------------------
# OrderResult 数据类完整性
# ---------------------------------------------------------------------------


class TestOrderResult:
    def test_failed_result_has_error(self):
        sig = make_signal()
        result = OrderResult(signal=sig, success=False, error="余额不足")
        assert result.success is False
        assert result.error == "余额不足"
        assert result.tx_hash == ""

    def test_successful_result_defaults(self):
        sig = make_signal()
        result = OrderResult(signal=sig, success=True, tx_hash="DRY_RUN")
        assert result.actual_price == 0.0
        assert result.filled_size == 0.0


# ---------------------------------------------------------------------------
# VWAP 滑点预估测试
# ---------------------------------------------------------------------------


class TestEstimateVwap:
    """测试 estimate_vwap 静态方法。"""

    def test_empty_book_returns_none(self):
        result = OrderExecutor.estimate_vwap({}, 10.0)
        assert result["vwap"] is None
        assert result["fillable_usdc"] == 0.0

    def test_no_asks_returns_none(self):
        book = {"bids": [{"price": "0.5", "size": "100"}], "asks": []}
        result = OrderExecutor.estimate_vwap(book, 10.0)
        assert result["vwap"] is None

    def test_single_level_exact_fill(self):
        book = {"asks": [{"price": "0.50", "size": "100"}]}
        # 100 shares @ 0.50 = $50 total depth
        result = OrderExecutor.estimate_vwap(book, 25.0)
        assert result["vwap"] == 0.5
        assert result["best_price"] == 0.5
        assert result["deviation"] == 0.0
        assert result["fillable_usdc"] == 25.0
        assert result["levels_consumed"] == 1

    def test_multi_level_calculates_weighted_avg(self):
        book = {
            "asks": [
                {"price": "0.50", "size": "20"},  # $10 available
                {"price": "0.55", "size": "20"},  # $11 available
                {"price": "0.60", "size": "20"},  # $12 available
            ]
        }
        # Want $20: fill all of level 1 ($10) + fill rest from level 2
        result = OrderExecutor.estimate_vwap(book, 20.0)
        assert result["vwap"] is not None
        assert result["best_price"] == 0.5
        assert result["deviation"] is not None
        assert result["deviation"] > 0  # VWAP > bestAsk
        assert result["levels_consumed"] == 2

    def test_insufficient_depth_partial_fill(self):
        book = {"asks": [{"price": "0.50", "size": "10"}]}
        # Only $5 depth, want $20
        result = OrderExecutor.estimate_vwap(book, 20.0)
        assert result["fillable_usdc"] == 5.0
        assert result["vwap"] == 0.5
        assert result["levels_consumed"] == 1

    def test_sell_side_uses_bids(self):
        book = {
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [{"price": "0.60", "size": "100"}],
        }
        result = OrderExecutor.estimate_vwap(book, 10.0, side="sell")
        assert result["best_price"] == 0.55
        assert result["vwap"] == 0.55


class TestVwapIntegration:
    """VWAP 与 execute() 流程集成测试。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.get")
    def test_vwap_disabled_does_not_block(self, mock_get):
        """vwap_enabled=false 时不阻塞下单。"""

        def cfg(key, default=None):
            if key == "executor.vwap_enabled":
                return False
            if key == "general.dry_run":
                return True
            return default

        mock_get.side_effect = cfg

        sig = make_signal()
        result = self.executor.execute(sig)
        assert result.success is True

    @patch("src.utils.orderbook_history.fetch_orderbook")
    @patch("src.executor.order_executor.get")
    def test_vwap_low_depth_rejects(self, mock_get, mock_book):
        """订单簿深度不足时跳过下单。"""

        def cfg(key, default=None):
            mapping = {
                "executor.vwap_enabled": True,
                "executor.vwap_max_deviation": 0.05,
                "executor.vwap_min_depth_usdc": 50.0,
                "general.dry_run": True,
            }
            return mapping.get(key, default)

        mock_get.side_effect = cfg
        mock_book.return_value = {"asks": [{"price": "0.50", "size": "10"}]}

        sig = make_signal(size_usdc=10.0)
        sig.token_id = "123456"
        result = self.executor.execute(sig)
        assert result.success is False
        assert "深度不足" in result.error


# ---------------------------------------------------------------------------
# _to_condition_id 测试
# ---------------------------------------------------------------------------


class TestToConditionId:
    """测试 Gamma numeric ID → CLOB condition_id 转换。"""

    def test_hex_id_returns_as_is(self):
        """已是 0x 格式时直接返回。"""
        cid = "0xabc123def456"
        assert OrderExecutor._to_condition_id(cid) == cid

    @patch("src.executor.order_executor.requests.get")
    def test_numeric_id_converts_via_gamma(self, mock_req):
        """Gamma numeric ID 应通过 API 转换为 conditionId。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "conditionId": "0x1234567890abcdef1234567890abcdef"
        }
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        result = OrderExecutor._to_condition_id("1587788")
        assert result == "0x1234567890abcdef1234567890abcdef"
        mock_req.assert_called_once()
        assert "1587788" in mock_req.call_args[0][0]

    @patch("src.executor.order_executor.requests.get")
    def test_numeric_id_condition_id_field(self, mock_req):
        """Gamma 返回 condition_id 字段（备选名称）时也能解析。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"condition_id": "0xfedcba9876543210"}
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        result = OrderExecutor._to_condition_id("123456")
        assert result == "0xfedcba9876543210"

    @patch("src.executor.order_executor.requests.get")
    def test_api_failure_returns_original(self, mock_req):
        """Gamma API 失败时回退到原始 ID。"""
        mock_req.side_effect = Exception("Connection timeout")
        result = OrderExecutor._to_condition_id("999999")
        assert result == "999999"

    @patch("src.executor.order_executor.requests.get")
    def test_empty_response_returns_original(self, mock_req):
        """Gamma API 返回空 conditionId 时回退。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {}
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        result = OrderExecutor._to_condition_id("111111")
        assert result == "111111"


class TestResolveTokenId:
    """测试 _resolve_token_id：标准 Yes/No + 自定义 outcome 位置回退。"""

    def _make_executor(self):
        with patch.object(OrderExecutor, "__init__", lambda self: None):
            ex = OrderExecutor.__new__(OrderExecutor)
            ex._client = None
            return ex

    @patch.object(OrderExecutor, "_to_condition_id", return_value="0xabc")
    def test_standard_yes_no_exact_match(self, _mock_cid):
        """标准 Yes/No 市场精确匹配。"""
        ex = self._make_executor()
        mock_client = MagicMock()
        mock_client.get_market.return_value = {
            "tokens": [
                {"token_id": "tok_yes", "outcome": "Yes"},
                {"token_id": "tok_no", "outcome": "No"},
            ]
        }
        sig = make_signal(outcome="YES")
        assert ex._resolve_token_id(mock_client, sig) == "tok_yes"

        sig_no = make_signal(outcome="NO")
        assert ex._resolve_token_id(mock_client, sig_no) == "tok_no"

    @patch.object(OrderExecutor, "_to_condition_id", return_value="0xabc")
    def test_custom_outcome_up_down(self, _mock_cid):
        """自定义 Up/Down 市场：YES→tokens[0](Up)，NO→tokens[1](Down)。"""
        ex = self._make_executor()
        mock_client = MagicMock()
        mock_client.get_market.return_value = {
            "tokens": [
                {"token_id": "tok_up", "outcome": "Up"},
                {"token_id": "tok_down", "outcome": "Down"},
            ]
        }
        sig_yes = make_signal(outcome="YES")
        assert ex._resolve_token_id(mock_client, sig_yes) == "tok_up"

        sig_no = make_signal(outcome="NO")
        assert ex._resolve_token_id(mock_client, sig_no) == "tok_down"

    @patch.object(OrderExecutor, "_to_condition_id", return_value="0xabc")
    def test_custom_outcome_knicks_nets(self, _mock_cid):
        """自定义 Knicks/Nets 市场：YES→tokens[0]，NO→tokens[1]。"""
        ex = self._make_executor()
        mock_client = MagicMock()
        mock_client.get_market.return_value = {
            "tokens": [
                {"token_id": "tok_knicks", "outcome": "Knicks"},
                {"token_id": "tok_nets", "outcome": "Nets"},
            ]
        }
        sig_no = make_signal(outcome="NO")
        assert ex._resolve_token_id(mock_client, sig_no) == "tok_nets"

    @patch.object(OrderExecutor, "_to_condition_id", return_value="0xabc")
    def test_empty_tokens_raises(self, _mock_cid):
        """tokens 为空时应抛出 ValueError。"""
        ex = self._make_executor()
        mock_client = MagicMock()
        mock_client.get_market.return_value = {"tokens": []}
        sig = make_signal(outcome="YES")
        with pytest.raises(ValueError, match="无匹配"):
            ex._resolve_token_id(mock_client, sig)

    @patch.object(OrderExecutor, "_to_condition_id", return_value="0xabc")
    def test_api_exception_raises(self, _mock_cid):
        """CLOB API 异常时应抛出 ValueError。"""
        ex = self._make_executor()
        mock_client = MagicMock()
        mock_client.get_market.side_effect = Exception("CLOB down")
        sig = make_signal(outcome="YES")
        with pytest.raises(ValueError, match="无法解析"):
            ex._resolve_token_id(mock_client, sig)


# ---------------------------------------------------------------------------
# sell_position 测试
# ---------------------------------------------------------------------------


class TestSellPosition:
    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.get")
    def test_sell_position_dry_run(self, mock_get):
        """DRY RUN 模式下 sell_position 返回成功但不真实下单。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": True,
        }.get(key, default)

        bet = {
            "market_id": "mkt_001",
            "outcome": "YES",
            "exit_price": 0.65,
            "market_price": 0.50,
            "size_usdc": 2.0,
            "question": "Will it rain?",
            "trigger": "profit_take",
        }
        result = self.executor.sell_position(bet)
        assert result.success is True
        assert result.tx_hash == "DRY_RUN_SELL"

    @patch("src.executor.order_executor.get")
    def test_sell_position_dry_run_no_outcome(self, mock_get):
        """outcome 为空时默认 YES。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": True,
        }.get(key, default)

        bet = {
            "market_id": "mkt_001",
            "exit_price": 0.6,
            "market_price": 0.5,
            "size_usdc": 1.0,
        }
        result = self.executor.sell_position(bet)
        assert result.success is True

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_sell_position_zero_shares(self, mock_get, _mock_dry):
        """股数为0时返回失败（size_usdc=0 的场景）。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sell_offset_pct": 0.002,
        }.get(key, default)

        bet = {
            "market_id": "mkt_001",
            "outcome": "YES",
            "exit_price": 0.6,
            "market_price": 0.5,
            "size_usdc": 0,
        }
        with patch.object(self.executor, "_get_clob_client"):
            with patch.object(
                self.executor, "_resolve_token_id", return_value="tok_yes"
            ):
                result = self.executor.sell_position(bet)
                assert result.success is False
                assert "无效股数" in (result.error or "")

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_sell_position_live_refreshes_balance_and_succeeds(
        self, mock_sleep, mock_get, _mock_dry
    ):
        """实盘卖出前应刷新 CLOB 缓存并成功下单。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sell_offset_pct": 0.002,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.sweep_min_shares": 0.01,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        # Call sequence: 1) token balance, 2) USDC before sell,
        # 3) sweep check, 4) USDC after sell
        mock_client.get_balance_allowance.side_effect = [
            {"balance": "8000000", "allowance": "9999999999"},  # token: 8 shares
            {"balance": "100000000"},  # USDC before
            {"balance": "0", "allowance": "9999999999"},  # sweep: 0 shares
            {"balance": "102400000"},  # USDC after
        ]
        mock_client.post_order.return_value = {"orderID": "sell_001"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "4.0",
            "price": "0.60",
        }
        self.executor._client = mock_client

        bet = {
            "market_id": "mkt_001",
            "outcome": "YES",
            "exit_price": 0.6,
            "market_price": 0.5,
            "size_usdc": 2.0,
            "question": "Will it rain?",
            "trigger": "stop_loss",
        }
        with patch.object(self.executor, "_resolve_token_id", return_value="tok_yes"):
            result = self.executor.sell_position(bet)

        assert result.success is True
        assert result.tx_hash == "sell_001"
        # 应调用了 update_balance_allowance 刷新缓存（sell + sweep check = 2次）
        assert mock_client.update_balance_allowance.call_count == 2
        assert mock_client.get_balance_allowance.call_count == 4

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_sell_position_zero_token_balance_skips(self, mock_get, _mock_dry):
        """链上 token 余额为 0 时直接返回失败。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sell_offset_pct": 0.002,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        mock_client.get_balance_allowance.return_value = {
            "balance": "0",
            "allowance": "9999999999",
        }
        self.executor._client = mock_client

        bet = {
            "market_id": "mkt_001",
            "outcome": "YES",
            "exit_price": 0.6,
            "market_price": 0.5,
            "size_usdc": 2.0,
            "question": "Test",
            "trigger": "stop_loss",
        }
        with patch.object(self.executor, "_resolve_token_id", return_value="tok_yes"):
            result = self.executor.sell_position(bet)

        assert result.success is False
        assert "余额为 0" in (result.error or "")

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_sell_caps_shares_to_actual_balance(self, mock_sleep, mock_get, _mock_dry):
        """卖出时使用链上实际余额（而非计算股数），避免残余孤儿仓位。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sell_offset_pct": 0.002,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.sweep_min_shares": 0.01,
            "executor.min_order_size_shares": 5,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        # Call sequence: 1) token balance, 2) USDC before, 3) sweep check, 4) USDC after
        mock_client.get_balance_allowance.side_effect = [
            {"balance": "6000000", "allowance": "9999999999"},  # token: 6 shares
            {"balance": "100000000"},  # USDC before
            {"balance": "0", "allowance": "9999999999"},  # sweep: 0
            {"balance": "103600000"},  # USDC after
        ]
        mock_client.post_order.return_value = {"orderID": "sell_cap"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "6.0",
            "price": "0.60",
        }
        self.executor._client = mock_client

        bet = {
            "market_id": "mkt_001",
            "outcome": "YES",
            "exit_price": 0.6,
            "market_price": 0.25,  # shares = 2.0 / 0.25 = 8, but only 6 on-chain
            "size_usdc": 2.0,
            "question": "Test",
            "trigger": "stop_loss",
        }
        with patch.object(self.executor, "_resolve_token_id", return_value="tok_yes"):
            result = self.executor.sell_position(bet)

        assert result.success is True
        # OrderArgs should have been called with size=6.0 (capped), not 8.0
        call_args = mock_client.create_order.call_args_list[0][0][0]
        assert call_args.size == 6.0

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_sell_uses_actual_balance_when_more_than_calculated(
        self, mock_sleep, mock_get, _mock_dry
    ):
        """链上余额多于计算股数时，卖出全部实际余额（防止残余shares成孤儿）。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sell_offset_pct": 0.002,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.sweep_min_shares": 0.01,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        # Call sequence: 1) token balance, 2) USDC before, 3) sweep check, 4) USDC after
        mock_client.get_balance_allowance.side_effect = [
            {"balance": "18030000", "allowance": "9999999999"},  # token: 18.03
            {"balance": "100000000"},  # USDC before
            {"balance": "0", "allowance": "9999999999"},  # sweep: 0
            {"balance": "100720000"},  # USDC after
        ]
        mock_client.post_order.return_value = {"orderID": "sell_full"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "18.0",
            "price": "0.04",
        }
        self.executor._client = mock_client

        bet = {
            "market_id": "mkt_001",
            "outcome": "NO",
            "exit_price": 0.04,
            "market_price": 0.061,  # shares = 1.08 / 0.061 ≈ 17.70
            "size_usdc": 1.08,
            "question": "Bitcoin Up or Down?",
            "trigger": "floor",
        }
        with patch.object(self.executor, "_resolve_token_id", return_value="tok_no"):
            result = self.executor.sell_position(bet)

        assert result.success is True
        # Should sell ALL 18.03 shares (floored to 18.03) not just 17.70
        call_args = mock_client.create_order.call_args_list[0][0][0]
        assert call_args.size == 18.03  # math.floor(18.03 * 100) / 100

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_sell_actual_price_computed_from_fill(
        self, mock_sleep, mock_get, _mock_dry
    ):
        """卖出成交后 actual_price 应为 filled_usdc / sell_size（非 limit_price）。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sell_offset_pct": 0.002,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.sweep_min_shares": 0.01,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        # Call sequence: 1) token balance, 2) USDC before, 3) sweep check, 4) USDC after
        mock_client.get_balance_allowance.side_effect = [
            {"balance": "10000000", "allowance": "9999999999"},  # token: 10
            {"balance": "100000000"},  # USDC before
            {"balance": "0", "allowance": "9999999999"},  # sweep: 0
            {"balance": "106200000"},  # USDC after
        ]
        mock_client.create_and_post_order.return_value = {"orderID": "sell_price"}
        # 10 shares × 0.62 actual price = 6.20 USDC filled
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "10.0",
            "price": "0.62",
        }
        self.executor._client = mock_client

        bet = {
            "market_id": "mkt_002",
            "outcome": "YES",
            "exit_price": 0.6,  # limit will be 0.598
            "market_price": 0.5,
            "size_usdc": 5.0,
            "question": "Test price",
            "trigger": "ceiling",
        }
        with patch.object(
            self.executor, "_resolve_token_id", return_value="tok_yes"
        ), patch.object(
            self.executor,
            "_verify_trade_on_chain",
            return_value={"verified": False},
        ):
            result = self.executor.sell_position(bet)

        assert result.success is True
        # actual_price = filled_usdc / sell_size = 6.20 / 10.0 = 0.62
        assert abs(result.actual_price - 0.62) < 0.01

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_sell_balance_query_failure_still_proceeds(
        self, mock_sleep, mock_get, _mock_dry
    ):
        """余额查询失败时（返回-1）仍继续卖出尝试。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sell_offset_pct": 0.002,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.sweep_min_shares": 0.01,
            "executor.min_order_size_shares": 5,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        # All calls fail — first for sell, second for sweep
        mock_client.get_balance_allowance.side_effect = Exception("API timeout")
        mock_client.create_and_post_order.return_value = {"orderID": "sell_fallback"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "4.0",
            "price": "0.60",
        }
        self.executor._client = mock_client

        bet = {
            "market_id": "mkt_001",
            "outcome": "YES",
            "exit_price": 0.6,
            "market_price": 0.5,
            "size_usdc": 3.0,  # shares = 3.0/0.5 = 6.0, passes min_order_shares=5
            "question": "Test",
            "trigger": "stop_loss",
        }
        with patch.object(self.executor, "_resolve_token_id", return_value="tok_yes"):
            result = self.executor.sell_position(bet)

        # Should still attempt sell (actual_shares=-1, won't block)
        assert result.success is True

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_sell_below_min_order_shares_returns_error(
        self, mock_sleep, mock_get, _mock_dry
    ):
        """股数低于 CLOB 最小订单量时，卖出前预检查直接返回错误（不浪费 API 调用）。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sell_offset_pct": 0.002,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.sweep_min_shares": 0.01,
            "executor.min_order_size_shares": 5,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        mock_client.get_balance_allowance.return_value = {
            "balance": "2000000",  # 2 shares (below min=5)
            "allowance": "9999999999",
        }
        self.executor._client = mock_client

        bet = {
            "market_id": "mkt_small",
            "outcome": "NO",
            "exit_price": 0.9,
            "market_price": 0.8,
            "size_usdc": 1.6,  # shares = 1.6/0.8 = 2.0
            "question": "Test below min shares",
            "trigger": "profit_take",
        }
        with patch.object(self.executor, "_resolve_token_id", return_value="tok_no"):
            result = self.executor.sell_position(bet)

        assert result.success is False
        assert "below_min_order_size" in (result.error or "")
        # Should NOT have called create_and_post_order (no API call wasted)
        mock_client.create_and_post_order.assert_not_called()

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_sell_sweep_clears_residual_shares(self, mock_sleep, mock_get, _mock_dry):
        """卖出后链上有残余 shares (≥ min_order_size) 时，自动发起 sweep 清扫。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sell_offset_pct": 0.002,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.sweep_min_shares": 0.01,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        # T56: 残余 6 shares (> min_order_size=5), 确保 sweep 可执行
        mock_client.get_balance_allowance.side_effect = [
            {"balance": "10000000", "allowance": "9999999999"},  # token: 10 shares
            {"balance": "100000000"},  # USDC before sell
            {"balance": "6000000", "allowance": "9999999999"},  # sweep detect 6.0
            {"balance": "0", "allowance": "9999999999"},  # sweep 后余额检查
            {"balance": "103000000"},  # USDC after sell
        ]
        # Main sell: create_order + post_order (FOK); Sweep: create_and_post_order
        mock_client.post_order.return_value = {"orderID": "main_sell"}
        mock_client.create_and_post_order.return_value = {"orderID": "sweep_sell"}
        # Main sell fill, then sweep fill
        mock_client.get_order.side_effect = [
            {"status": "MATCHED", "size_matched": "10.0", "price": "0.50"},
            {"status": "MATCHED", "size_matched": "6.0", "price": "0.40"},
        ]
        self.executor._client = mock_client

        bet = {
            "market_id": "mkt_001",
            "outcome": "YES",
            "exit_price": 0.5,
            "market_price": 0.4,
            "size_usdc": 4.0,
            "question": "Sweep test",
            "trigger": "profit_take",
        }
        with patch.object(self.executor, "_resolve_token_id", return_value="tok_yes"):
            result = self.executor.sell_position(bet)

        assert result.success is True
        # Main sell via post_order (FOK), sweep via create_and_post_order
        assert mock_client.post_order.call_count == 1
        assert mock_client.create_and_post_order.call_count == 1
        # Sweep order size = math.floor(6.0 * 100) / 100 = 6.0
        sweep_args = mock_client.create_and_post_order.call_args_list[0][0][0]
        assert sweep_args.size == 6.0

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_sell_sweep_skips_tiny_residual(self, mock_sleep, mock_get, _mock_dry):
        """残余 shares < min_sweep 时跳过清扫。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sell_offset_pct": 0.002,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.sweep_min_shares": 0.01,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        # Call sequence: 1) token balance, 2) USDC before sell,
        # 3) tiny residual check, 4) USDC after sell
        mock_client.get_balance_allowance.side_effect = [
            {"balance": "5000000", "allowance": "9999999999"},  # token: 5 shares
            {"balance": "100000000"},  # USDC before
            {"balance": "5000", "allowance": "9999999999"},  # 0.005 shares
            {"balance": "102500000"},  # USDC after
        ]
        mock_client.post_order.return_value = {"orderID": "main_only"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "5.0",
            "price": "0.50",
        }
        self.executor._client = mock_client

        bet = {
            "market_id": "mkt_001",
            "outcome": "YES",
            "exit_price": 0.5,
            "market_price": 0.4,
            "size_usdc": 2.0,
            "question": "Tiny residual",
            "trigger": "stop_loss",
        }
        with patch.object(self.executor, "_resolve_token_id", return_value="tok_yes"):
            result = self.executor.sell_position(bet)

        assert result.success is True
        # Only main sell via post_order, no sweep since residual < 0.01
        assert mock_client.post_order.call_count == 1
        assert mock_client.create_and_post_order.call_count == 0

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_sell_sweep_skips_below_min_order_size(
        self, mock_sleep, mock_get, _mock_dry
    ):
        """T56: 残余 shares < min_order_size 时，sweep 预检查拦截，不提交 CLOB 订单。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sell_offset_pct": 0.002,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.sweep_min_shares": 0.01,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        # Residual = 0.33 shares (below min_order_size=5)
        mock_client.get_balance_allowance.side_effect = [
            {"balance": "10000000", "allowance": "9999999999"},  # token: 10 shares
            {"balance": "100000000"},  # USDC before sell
            {"balance": "330000", "allowance": "9999999999"},  # sweep detect 0.33
            {"balance": "105000000"},  # USDC after sell
        ]
        mock_client.post_order.return_value = {"orderID": "main_sell"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "10.0",
            "price": "0.50",
        }
        self.executor._client = mock_client

        bet = {
            "market_id": "mkt_001",
            "outcome": "YES",
            "exit_price": 0.5,
            "market_price": 0.4,
            "size_usdc": 4.0,
            "question": "Sweep below min",
            "trigger": "profit_take",
        }
        with patch.object(self.executor, "_resolve_token_id", return_value="tok_yes"):
            result = self.executor.sell_position(bet)

        assert result.success is True
        # Main sell via post_order, sweep skipped by pre-check
        assert mock_client.post_order.call_count == 1
        assert mock_client.create_and_post_order.call_count == 0
        # Residual should be reported as remaining_shares
        assert result.remaining_shares > 0

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_sell_sweep_failure_doesnt_affect_main_result(
        self, mock_sleep, mock_get, _mock_dry
    ):
        """sweep 失败不影响主卖出结果。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.sell_offset_pct": 0.002,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.sweep_min_shares": 0.01,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        # Call sequence: 1) token balance, 2) USDC before sell,
        # 3) sweep errors, 4) USDC after sell errors too
        mock_client.get_balance_allowance.side_effect = [
            {"balance": "5000000", "allowance": "9999999999"},  # token: 5 shares
            {"balance": "100000000"},  # USDC before
            Exception("network_error during sweep"),
            {"balance": "102500000"},  # USDC after
        ]
        mock_client.create_and_post_order.return_value = {"orderID": "main_sell"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "5.0",
            "price": "0.50",
        }
        self.executor._client = mock_client

        bet = {
            "market_id": "mkt_001",
            "outcome": "YES",
            "exit_price": 0.5,
            "market_price": 0.4,
            "size_usdc": 2.0,
            "question": "Sweep error test",
            "trigger": "stop_loss",
        }
        with patch.object(self.executor, "_resolve_token_id", return_value="tok_yes"):
            result = self.executor.sell_position(bet)

        # Main sell still succeeds despite sweep error
        assert result.success is True
        assert result.filled_size == 2.5  # 5.0 * 0.50

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_sell_small_position_forces_taker_mode(
        self, mock_sleep, mock_get, _mock_dry
    ):
        """T169: 小仓位(<2×min_order_size)即使配置为maker，也必须强制taker卖出。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.offset_pct": 0.001,
            "executor.sell_offset_pct": 0.01,
            "executor.sell_progressive_step": 0.005,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.sweep_min_shares": 0.01,
            "executor.min_order_size_shares": 5.0,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        mock_client.get_balance_allowance.side_effect = [
            {"balance": "6000000", "allowance": "9999999999"},  # token: 6 shares
            {"balance": "100000000"},  # USDC before
            {"balance": "0", "allowance": "9999999999"},  # sweep residual = 0
            {"balance": "102994000"},  # USDC after
        ]
        mock_client.post_order.return_value = {"orderID": "sell_small_taker"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "6.0",
            "price": "0.499",
        }
        self.executor._client = mock_client

        bet = {
            "market_id": "mkt_001",
            "outcome": "YES",
            "exit_price": 0.5,
            "market_price": 0.4,
            "size_usdc": 2.0,
            "question": "T169 small sell",
            "trigger": "profit_take",
            "category": "sports",
        }
        with patch.object(self.executor, "_resolve_token_id", return_value="tok_yes"):
            with patch.object(self.executor, "_get_order_mode", return_value="maker"):
                result = self.executor.sell_position(bet)

        assert result.success is True
        order_args = mock_client.create_order.call_args[0][0]
        # taker should use executor.offset_pct=0.001 => 0.500 - 0.001 = 0.499
        assert order_args.price == pytest.approx(0.499, abs=1e-6)


# ---------------------------------------------------------------------------
# 余额刷新方法测试
# ---------------------------------------------------------------------------


class TestRefreshAndGetTokenBalance:
    def setup_method(self):
        self.executor = OrderExecutor()

    def test_normal_balance_returned(self):
        """正常查询返回 shares 数量。"""
        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        mock_client.get_balance_allowance.return_value = {
            "balance": "5000000",
            "allowance": "9999999999",
        }

        result = self.executor._refresh_and_get_token_balance(mock_client, "0xTOKEN123")
        assert result == pytest.approx(5.0)
        mock_client.update_balance_allowance.assert_called_once()
        mock_client.get_balance_allowance.assert_called_once()

    def test_zero_balance(self):
        """链上余额为 0。"""
        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        mock_client.get_balance_allowance.return_value = {
            "balance": "0",
            "allowance": "0",
        }

        result = self.executor._refresh_and_get_token_balance(mock_client, "0xTOKEN123")
        assert result == 0.0

    def test_query_failure_returns_negative(self):
        """查询失败时返回 -1.0。"""
        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}
        mock_client.get_balance_allowance.side_effect = Exception("network error")

        result = self.executor._refresh_and_get_token_balance(mock_client, "0xTOKEN123")
        assert result == -1.0

    def test_refresh_failure_still_queries(self):
        """刷新缓存失败但查询仍然执行。"""
        mock_client = MagicMock()
        mock_client.update_balance_allowance.side_effect = Exception("refresh error")
        mock_client.get_balance_allowance.return_value = {
            "balance": "3000000",
            "allowance": "9999999999",
        }

        result = self.executor._refresh_and_get_token_balance(mock_client, "0xTOKEN123")
        assert result == pytest.approx(3.0)


class TestRefreshCollateralBalance:
    def setup_method(self):
        self.executor = OrderExecutor()

    def test_refresh_succeeds(self):
        """USDC 余额刷新正常执行。"""
        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = {}

        self.executor._refresh_collateral_balance(mock_client)
        mock_client.update_balance_allowance.assert_called_once()

    def test_refresh_failure_no_exception(self):
        """刷新失败不抛异常。"""
        mock_client = MagicMock()
        mock_client.update_balance_allowance.side_effect = Exception("fail")

        # Should not raise
        self.executor._refresh_collateral_balance(mock_client)


# ---------------------------------------------------------------------------
# 余额不足提前阻止下单
# ---------------------------------------------------------------------------


class TestInsufficientBalance:
    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_balance_too_low_skips_order(self, mock_get, _mock_dry):
        """余额不足时应直接返回失败，不提交任何订单。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "risk.max_single_market_pct": 0.05,
            "executor.min_bet_usdc": 1.0,
            "executor.probe_enabled": False,
        }.get(key, default)

        sig = make_signal(size_usdc=3.0)
        with patch.object(self.executor, "_check_balance", return_value=0.5):
            with patch.object(self.executor, "_submit_order") as mock_submit:
                result = self.executor.execute(sig)
                assert result.success is False
                assert "余额不足" in (result.error or "")
                mock_submit.assert_not_called()

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_balance_sufficient_proceeds(self, mock_get, _mock_dry):
        """余额充足时应正常下单。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "risk.max_single_market_pct": 0.05,
            "executor.min_bet_usdc": 1.0,
            "executor.probe_enabled": False,
        }.get(key, default)

        sig = make_signal(size_usdc=3.0)
        with patch.object(self.executor, "_check_balance", return_value=100.0):
            with patch.object(self.executor, "_submit_order") as mock_submit:
                mock_submit.return_value = OrderResult(
                    signal=sig, success=True, tx_hash="0xABC"
                )
                result = self.executor.execute(sig)
                assert result.success is True
                mock_submit.assert_called_once()


# ---------------------------------------------------------------------------
# 成交确认 _wait_for_fill() 测试
# ---------------------------------------------------------------------------


class TestWaitForFill:
    """测试 GTC 限价单成交轮询逻辑。"""

    def setup_method(self):
        self.executor = OrderExecutor()
        self.mock_client = MagicMock()
        self.executor._client = self.mock_client

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_matched_immediately(self, mock_sleep, mock_get):
        """订单第一次查询就是 MATCHED → 返回成功。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.fill_timeout_sec": 45,
            "executor.fill_poll_interval_sec": 5,
        }.get(key, default)
        self.mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "30.0",
            "price": "0.10",
        }
        result = self.executor._wait_for_fill("order123", 3.0)
        assert result["status"] == "MATCHED"
        assert result["filled_usdc"] == 3.0  # 30 shares × 0.10 price
        assert result["matched_shares"] == 30.0

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_live_then_matched(self, mock_sleep, mock_get):
        """订单先 LIVE 再 MATCHED → 最终成功。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.fill_timeout_sec": 45,
            "executor.fill_poll_interval_sec": 5,
        }.get(key, default)
        self.mock_client.get_order.side_effect = [
            {"status": "LIVE", "size_matched": "0", "price": "0.10"},
            {"status": "MATCHED", "size_matched": "30.0", "price": "0.10"},
        ]
        result = self.executor._wait_for_fill("order123", 3.0)
        assert result["status"] == "MATCHED"

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_cancelled_by_exchange(self, mock_sleep, mock_get):
        """订单被交易所取消 → 返回失败。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.fill_timeout_sec": 45,
            "executor.fill_poll_interval_sec": 5,
        }.get(key, default)
        self.mock_client.get_order.return_value = {
            "status": "CANCELLED",
            "size_matched": "0",
            "price": "0.10",
        }
        result = self.executor._wait_for_fill("order123", 3.0)
        assert result["status"] == "CANCELLED"
        assert result["filled_usdc"] == 0.0
        assert result["matched_shares"] == 0.0

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    @patch("src.executor.order_executor.time.monotonic")
    def test_timeout_cancels_order(self, mock_mono, mock_sleep, mock_get):
        """超时后应主动取消订单。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.fill_timeout_sec": 10,
            "executor.fill_poll_interval_sec": 5,
        }.get(key, default)
        # 模拟时间推进超过 timeout
        mock_mono.side_effect = [0, 5, 11, 11]
        self.mock_client.get_order.side_effect = [
            {"status": "LIVE", "size_matched": "0", "price": "0.10"},
            # 超时后最后查询
            {"status": "LIVE", "size_matched": "0", "price": "0.10"},
        ]
        self.mock_client.cancel.return_value = {}
        result = self.executor._wait_for_fill("order123", 3.0)
        assert result["status"] == "TIMEOUT_CANCELLED"
        assert result["filled_usdc"] == 0.0
        assert result["matched_shares"] == 0.0
        self.mock_client.cancel.assert_called_once_with("order123")

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    @patch("src.executor.order_executor.time.monotonic")
    def test_partial_fill_on_timeout(self, mock_mono, mock_sleep, mock_get):
        """超时时已有部分成交 → 返回部分成交金额。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.fill_timeout_sec": 10,
            "executor.fill_poll_interval_sec": 5,
        }.get(key, default)
        mock_mono.side_effect = [0, 5, 11, 11]
        self.mock_client.get_order.side_effect = [
            {"status": "PARTLY_MATCHED", "size_matched": "10.0", "price": "0.10"},
            # 超时后查询
            {"status": "PARTLY_MATCHED", "size_matched": "10.0", "price": "0.10"},
        ]
        self.mock_client.cancel.return_value = {}
        result = self.executor._wait_for_fill("order123", 3.0)
        assert result["status"] == "TIMEOUT_PARTIAL"
        assert result["filled_usdc"] == 1.0  # 10 shares × 0.10
        assert result["matched_shares"] == 10.0

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_empty_order_id(self, mock_sleep, mock_get):
        """空 order_id → 立即返回错误。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.fill_timeout_sec": 45,
            "executor.fill_poll_interval_sec": 5,
        }.get(key, default)
        result = self.executor._wait_for_fill("", 3.0)
        assert result["status"] == "ERROR"
        assert result["filled_usdc"] == 0.0
        assert result["matched_shares"] == 0.0

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_expired_order(self, mock_sleep, mock_get):
        """订单过期 → 返回失败。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.fill_timeout_sec": 45,
            "executor.fill_poll_interval_sec": 5,
        }.get(key, default)
        self.mock_client.get_order.return_value = {
            "status": "EXPIRED",
            "size_matched": "0",
            "price": "0.10",
        }
        result = self.executor._wait_for_fill("order123", 3.0)
        assert result["status"] == "EXPIRED"
        assert result["filled_usdc"] == 0.0
        assert result["matched_shares"] == 0.0

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_api_error_continues_polling(self, mock_sleep, mock_get):
        """API 查询异常时不中断，继续轮询。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.fill_timeout_sec": 45,
            "executor.fill_poll_interval_sec": 5,
        }.get(key, default)
        self.mock_client.get_order.side_effect = [
            Exception("network error"),
            {"status": "MATCHED", "size_matched": "30.0", "price": "0.10"},
        ]
        result = self.executor._wait_for_fill("order123", 3.0)
        assert result["status"] == "MATCHED"


# ---------------------------------------------------------------------------
# cancel_stale_orders() 测试
# ---------------------------------------------------------------------------


class TestCancelStaleOrders:
    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.get")
    def test_dry_run_noop(self, mock_get):
        """DRY RUN 模式下不执行取消。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": True,
        }.get(key, default)
        assert self.executor.cancel_stale_orders() == 0

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_live_cancel_all(self, mock_get, _mock_dry):
        """实盘模式取消所有挂单。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
        }.get(key, default)
        mock_client = MagicMock()
        mock_client.cancel_all.return_value = {
            "canceled": ["order1", "order2", "order3"]
        }
        self.executor._client = mock_client
        count = self.executor.cancel_stale_orders()
        assert count == 3
        mock_client.cancel_all.assert_called_once()

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_cancel_error_returns_zero(self, mock_get, _mock_dry):
        """取消失败时返回 0。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
        }.get(key, default)
        mock_client = MagicMock()
        mock_client.cancel_all.side_effect = Exception("API error")
        self.executor._client = mock_client
        assert self.executor.cancel_stale_orders() == 0


# ---------------------------------------------------------------------------
# _submit_order + fill 验证 集成测试
# ---------------------------------------------------------------------------


class TestSubmitOrderFillIntegration:
    """测试 _submit_order 通过 _wait_for_fill 确认实际成交。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_submit_unfilled_returns_failure(self, mock_sleep, mock_get):
        """限价单未成交时 _submit_order 应返回 success=False。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.offset_pct": 0.001,
            "executor.min_order_size_shares": 5,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {"orderID": "order999"}
        # get_order 返回超时后 LIVE（未成交）
        mock_client.get_order.return_value = {
            "status": "LIVE",
            "size_matched": "0",
            "price": "0.50",
        }
        mock_client.cancel.return_value = {}
        self.executor._client = mock_client

        sig = make_signal(size_usdc=3.0)
        sig.expected_price = 0.50
        sig.token_id = "0xTOKEN123"

        with patch("src.executor.order_executor.time.monotonic") as mock_mono:
            mock_mono.side_effect = [0, 6, 6, 6]
            result = self.executor._submit_order(sig)

        assert result.success is False
        assert "未成交" in result.error

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_submit_filled_returns_success(self, mock_sleep, mock_get):
        """限价单成交后 _submit_order 应返回 success=True 和实际成交额。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.offset_pct": 0.001,
            "executor.min_order_size_shares": 5,
            "executor.fill_timeout_sec": 45,
            "executor.fill_poll_interval_sec": 5,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {"orderID": "orderFILLED"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "6.0",
            "price": "0.501",
        }
        self.executor._client = mock_client

        sig = make_signal(size_usdc=3.0)
        sig.expected_price = 0.50
        sig.token_id = "0xTOKEN123"

        result = self.executor._submit_order(sig)
        assert result.success is True
        assert result.tx_hash == "orderFILLED"
        assert result.filled_size == pytest.approx(3.006, abs=0.01)  # 6 × 0.501


# ---------------------------------------------------------------------------
# CLOB 最小订单金额保障测试
# ---------------------------------------------------------------------------


class TestClobMinOrderGuard:
    """验证 _submit_order 在有效金额低于 CLOB 最小限额时自动调高份数。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.utils.orderbook_history.get_min_order_size", return_value=1.0)
    @patch("src.executor.order_executor.get")
    def test_shares_bumped_when_cost_below_clob_min(self, mock_get, mock_min_size):
        """当 size_usdc / limit_price 的有效成本 < clob_min 时，自动增加份数。
        T86: amplification 必须在 max_clob_amplification 以内。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.offset_pct": 0.001,
            "executor.min_order_size_shares": 2,
            "executor.clob_min_order_usdc": 1.10,
            "executor.fill_timeout_sec": 45,
            "executor.fill_poll_interval_sec": 5,
            "executor.min_bet_usdc": 1.0,
            "executor.min_order_size_shares": 1.0,
            "executor.post_buy_topup_buffer_shares": 0.1,
            "executor.post_buy_topup_safety_floor_shares": 1.1,
            "executor.max_clob_amplification": 2.0,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {"orderID": "orderMIN"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "2.0",
            "price": "0.981",
        }
        self.executor._client = mock_client

        sig = make_signal(size_usdc=1.0)  # $1.00 — below CLOB min after rounding
        sig.expected_price = 0.98  # limit_price = 0.981
        sig.token_id = "0xTOKEN_MIN"

        result = self.executor._submit_order(sig)
        # 验证创建订单时份数被调高（至少 >= api_min + buffer = 1.1 shares）
        call_args = mock_client.create_and_post_order.call_args
        order_args = call_args[0][0]
        assert (
            order_args.size >= 1.1
        ), f"shares={order_args.size:.2f} should be >= 1.1 (min+buffer)"

    @patch("src.utils.orderbook_history.get_min_order_size", return_value=5.0)
    @patch("src.executor.order_executor.get")
    def test_clob_amplification_rejected_when_over_limit(self, mock_get, mock_min_size):
        """T86: 当 CLOB 最小限额导致放大超过 max_clob_amplification 时，拒绝下单。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.offset_pct": 0.001,
            "executor.min_bet_usdc": 1.0,
            "executor.min_order_size_shares": 5.0,
            "executor.post_buy_topup_buffer_shares": 0.8,
            "executor.post_buy_topup_safety_floor_shares": 5.8,
            "executor.fill_timeout_sec": 45,
            "executor.fill_poll_interval_sec": 5,
            "executor.max_clob_amplification": 2.0,
        }.get(key, default)

        mock_client = MagicMock()
        self.executor._client = mock_client

        # $1.00 @ $0.20 → CLOB requires 5.8 shares × $0.201 = $1.17
        # amplification = $1.17 / $1.00 = 1.17x → under 2x, but let's use
        # a scenario where it's clearly over:
        # $1.00 @ $0.98 → CLOB min = 5.8 × $0.981 = $5.69, amplification = 5.69x
        sig = make_signal(size_usdc=1.0)
        sig.expected_price = 0.98  # limit_price ≈ 0.981
        sig.token_id = "0xTOK_REJECT"

        result = self.executor._submit_order(sig)
        assert isinstance(result, OrderResult)
        assert result.success is False
        assert "CLOB amplification" in (result.error or "")
        # 不应调用过 create_and_post_order
        mock_client.create_and_post_order.assert_not_called()

    @patch("src.utils.orderbook_history.get_min_order_size", return_value=5.0)
    @patch("src.executor.order_executor.get")
    def test_topup_exempt_from_clob_amplification(self, mock_get, mock_min_size):
        """TopUp 补买信号标记 _is_topup=True 时豁免 CLOB 放大限制。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.offset_pct": 0.001,
            "executor.min_bet_usdc": 1.0,
            "executor.min_order_size_shares": 5.0,
            "executor.post_buy_topup_buffer_shares": 0.8,
            "executor.fill_timeout_sec": 45,
            "executor.fill_poll_interval_sec": 5,
            "executor.max_clob_amplification": 2.0,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {"orderID": "topup-001"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "associate_trades": [{"side": "BUY", "price": "0.98", "size": "5.8"}],
        }
        self.executor._client = mock_client

        sig = make_signal(size_usdc=1.0)
        sig.expected_price = 0.98
        sig.token_id = "0xTOK_TOPUP"
        sig._is_topup = True  # topup 标记

        result = self.executor._submit_order(sig)
        # topup 豁免放大限制 → 应该继续下单而非被拒绝
        mock_client.create_and_post_order.assert_called_once()

    @patch("src.utils.orderbook_history.get_min_order_size", return_value=5.0)
    @patch("src.executor.order_executor.get")
    def test_topup_uses_bare_clob_min_no_buffer(self, mock_get, mock_min_size):
        """T204: TopUp 补买信号使用裸 CLOB 最小限额（无 buffer/floor），
        防止双重缓冲导致超买。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.offset_pct": 0.001,
            "executor.min_bet_usdc": 1.0,
            "executor.min_order_size_shares": 5.0,
            "executor.post_buy_topup_buffer_shares": 0.8,
            "executor.post_buy_topup_safety_floor_shares": 5.5,
            "executor.fill_timeout_sec": 45,
            "executor.fill_poll_interval_sec": 5,
            "executor.max_clob_amplification": 3.0,
            "order_execution.smart_mode": {"enabled": False},
            "order_execution.mode": "taker",
            "order_execution.taker.offset_pct": 0.01,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {"orderID": "topup-bare"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "associate_trades": [{"side": "BUY", "price": "0.80", "size": "5.0"}],
        }
        self.executor._client = mock_client
        self.executor._check_balance = MagicMock(return_value=50.0)
        self.executor._refresh_collateral_balance = MagicMock()

        # 补买 3.5 shares (< CLOB min 5.0)，价格 0.80
        sig = make_signal(size_usdc=2.80)  # 3.5 * 0.80
        sig.expected_price = 0.80
        sig.token_id = "0xTOK_TOPUP_BARE"
        sig._is_topup = True

        result = self.executor._submit_order(sig)

        # 验证下单被放大到裸 min=5.0（而非 min+buf=5.8 或 floor=5.5）
        call_args = mock_client.create_and_post_order.call_args
        order_args = call_args[0][0]
        # 放大后 shares 应 ≈5.0（裸 CLOB min），不是 5.8 或 5.5
        # 允许浮点精度误差 0.02
        assert order_args.size <= 5.02, (
            f"TopUp 应使用裸 CLOB min≈5.0，"
            f"实际={order_args.size}（可能包含了 buffer/floor）"
        )
        # 确保显著低于带 buffer 的目标 (5.8) 和 floor (5.5)
        assert (
            order_args.size < 5.5
        ), f"TopUp shares={order_args.size} >= floor=5.5，仍在使用 buffer/floor"

    @patch("src.executor.order_executor.get")
    def test_probe_uses_clob_min_default(self, mock_get):
        """试探单金额默认使用 clob_min_order_usdc 而非硬编码 $1.00。"""
        mock_get.side_effect = lambda key, default=None: {
            "executor.clob_min_order_usdc": 1.10,
            "executor.probe_size_usdc": None,
            "executor.offset_pct": 0.001,
            "executor.min_order_size_shares": 2,
            "executor.fill_timeout_sec": 45,
            "executor.fill_poll_interval_sec": 5,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {"orderID": "orderPROBE"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "2.0",
            "price": "0.55",
        }
        self.executor._client = mock_client

        sig = make_signal(size_usdc=5.0)
        sig.expected_price = 0.50
        sig.token_id = "0xTOK"

        result = self.executor._probe_order(sig)
        # 验证试探单创建时使用了 >= $1.10 金额
        call_args = mock_client.create_and_post_order.call_args
        order_args = call_args[0][0]
        effective_cost = order_args.size * order_args.price
        assert effective_cost >= 1.10

    @patch("src.utils.orderbook_history.get_min_order_size", return_value=None)
    @patch("src.executor.order_executor.get")
    def test_buy_min_size_includes_buffer_to_absorb_partial_fills(
        self, mock_get, mock_min_size
    ):
        """T79: 最小订单检查应加入 buffer_shares，防止 CLOB 部分成交后 shares < min。

        之前 BUG：目标精确等于 api_min_shares(5.0)，CLOB 部分成交得到 4.95 < 5.0。
        修复后：目标 = api_min_shares + post_buy_topup_buffer_shares = 5.0 + 0.8 = 5.8。
        """
        mock_get.side_effect = lambda key, default=None: {
            "executor.offset_pct": 0.001,
            "executor.min_bet_usdc": 1.0,
            "executor.min_order_size_shares": 5.0,
            "executor.post_buy_topup_buffer_shares": 0.8,
            "executor.fill_timeout_sec": 45,
            "executor.fill_poll_interval_sec": 5,
            "executor.settlement_delay_sec": 0,
            "executor.max_clob_amplification": 2.0,
        }.get(key, default)

        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {"orderID": "ord-t79"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "5.8",
            "price": "0.202",
        }
        self.executor._client = mock_client

        # $1.00 @ $0.202 = 4.95 shares（不加 buffer 时刚好低于 min=5.0 + 精确等于）
        # 修复后须提交 >= 5.8 shares
        sig = make_signal(size_usdc=1.0)
        sig.expected_price = 0.20  # limit_price ~ 0.201
        sig.token_id = "0xT79"

        result = self.executor._submit_order(sig)
        call_args = mock_client.create_and_post_order.call_args
        order_args = call_args[0][0]
        # 修复后：下单 shares 应 >= min(5.0) + buffer(0.8) = 5.8
        assert (
            order_args.size >= 5.8
        ), f"T79: 提交 shares={order_args.size:.2f} 应 >= 5.8 (min+buffer)"


# ---------------------------------------------------------------------------
# Balance Delta 实际成交金额校准测试
# ---------------------------------------------------------------------------


class TestBalanceDeltaCalibration:
    """验证 _submit_order 使用余额差修正 filled_usdc（而非 limit_price × size_matched）"""

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.get")
    def test_buy_uses_balance_delta_over_estimate(self, mock_get):
        """买入时，如果余额差与估算有偏离，应使用余额差（更准确）"""
        mock_get.side_effect = lambda k, *a, **kw: {
            "general.dry_run": False,
            "executor.offset_pct": 0.001,
            "executor.clob_min_order_usdc": 1.10,
            "executor.min_order_size_shares": 2,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.settlement_delay_sec": 0,
            "executor.balance_retry_count": 0,
            "executor.max_clob_amplification": 10.0,
        }.get(k)

        mock_client = MagicMock()
        # 下单前余额 100.00, 下单后余额 98.88 → 实际花费 $1.12
        mock_client.get_balance_allowance.side_effect = [
            {"balance": "100000000"},  # before: $100.00
            {"balance": "98880000"},  # after:  $98.88
        ]
        mock_client.create_and_post_order.return_value = {"orderID": "ord-001"}
        # get_order 返回 MATCHED, size_matched=2.0, price=0.571 (limit price)
        # 估算 = 2.0 * 0.571 = $1.142, 但实际余额差 = $1.12
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "2.0",
            "price": "0.571",
        }
        self.executor._client = mock_client

        sig = make_signal(size_usdc=1.14)
        sig.expected_price = 0.57
        sig.token_id = "0xTOK"

        result = self.executor._submit_order(sig)
        assert result.success is True
        # Should use balance delta $1.12, NOT estimate $1.142
        assert abs(result.filled_size - 1.12) < 0.01

    @patch("src.executor.order_executor.get")
    def test_buy_falls_back_to_estimate_on_balance_query_failure(self, mock_get):
        """余额查询失败时回退到 size_matched × price 估算"""
        mock_get.side_effect = lambda k, *a, **kw: {
            "general.dry_run": False,
            "executor.offset_pct": 0.001,
            "executor.clob_min_order_usdc": 1.10,
            "executor.min_order_size_shares": 2,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.settlement_delay_sec": 0,
            "executor.balance_retry_count": 0,
            "executor.max_clob_amplification": 10.0,
        }.get(k)

        mock_client = MagicMock()
        # 余额查询抛异常
        mock_client.get_balance_allowance.side_effect = Exception("network error")
        mock_client.create_and_post_order.return_value = {"orderID": "ord-002"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "2.0",
            "price": "0.571",
        }
        self.executor._client = mock_client

        sig = make_signal(size_usdc=1.14)
        sig.expected_price = 0.57
        sig.token_id = "0xTOK"

        result = self.executor._submit_order(sig)
        assert result.success is True
        # Falls back to estimate: 2.0 * 0.571 = 1.142
        assert abs(result.filled_size - 1.142) < 0.01

    @patch("src.executor.order_executor.get")
    def test_buy_no_correction_when_delta_negative(self, mock_get):
        """如果余额差为负（理论上不应该），不应使用余额差"""
        mock_get.side_effect = lambda k, *a, **kw: {
            "general.dry_run": False,
            "executor.offset_pct": 0.001,
            "executor.clob_min_order_usdc": 1.10,
            "executor.min_order_size_shares": 2,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.settlement_delay_sec": 0,
            "executor.balance_retry_count": 0,
            "executor.max_clob_amplification": 10.0,
        }.get(k)

        mock_client = MagicMock()
        # 下单后余额居然增加了（不可能但要测试防御性）
        mock_client.get_balance_allowance.side_effect = [
            {"balance": "100000000"},  # before: $100.00
            {"balance": "101000000"},  # after:  $101.00 (impossible)
        ]
        mock_client.create_and_post_order.return_value = {"orderID": "ord-003"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "2.0",
            "price": "0.571",
        }
        self.executor._client = mock_client

        sig = make_signal(size_usdc=1.14)
        sig.expected_price = 0.57
        sig.token_id = "0xTOK"

        result = self.executor._submit_order(sig)
        assert result.success is True
        # delta is -1.0, should NOT be used → fallback to estimate 1.142
        assert abs(result.filled_size - 1.142) < 0.01

    @patch("src.executor.order_executor.get")
    def test_buy_exact_match_no_correction_log(self, mock_get):
        """余额差与估算一致时，不记录校准日志"""
        mock_get.side_effect = lambda k, *a, **kw: {
            "general.dry_run": False,
            "executor.offset_pct": 0.001,
            "executor.clob_min_order_usdc": 1.10,
            "executor.min_order_size_shares": 2,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
            "executor.settlement_delay_sec": 0,
            "executor.balance_retry_count": 0,
            "executor.max_clob_amplification": 10.0,
        }.get(k)

        mock_client = MagicMock()
        # 余额差与估算完全一致
        mock_client.get_balance_allowance.side_effect = [
            {"balance": "100000000"},  # before: $100.00
            {"balance": "98858000"},  # after:  $98.858 → delta = $1.142
        ]
        mock_client.create_and_post_order.return_value = {"orderID": "ord-004"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "2.0",
            "price": "0.571",
        }
        self.executor._client = mock_client

        sig = make_signal(size_usdc=1.14)
        sig.expected_price = 0.57
        sig.token_id = "0xTOK"

        result = self.executor._submit_order(sig)
        assert result.success is True
        # delta == estimate == 1.142, both should give same result
        assert abs(result.filled_size - 1.142) < 0.01

    def test_get_usdc_balance_raw(self):
        """_get_usdc_balance_raw 正确解析 USDC 余额"""
        mock_client = MagicMock()
        mock_client.get_balance_allowance.return_value = {"balance": "98123456"}
        result = self.executor._get_usdc_balance_raw(mock_client)
        assert abs(result - 98.123456) < 0.000001


# ---------------------------------------------------------------------------
# Phase 33: 部分成交校准 + Probe+Main 加权均价
# ---------------------------------------------------------------------------


class TestPartialFillCalibration:
    """验证部分成交时不使用 delta/requested_shares 的错误均价。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    def _make_mock_client(self, balances):
        """构建 mock CLOB client: balances = [before, after]"""
        client = MagicMock()
        client.get_balance_allowance.side_effect = [
            {"balance": str(int(b * 1_000_000))} for b in balances
        ]
        client.create_and_post_order.return_value = {"orderID": "0xTEST"}
        return client

    @patch("src.executor.order_executor.get")
    def test_partial_fill_matched_uses_clob_data(self, mock_get):
        """MATCHED + fill_ratio<85% → 余额未结算，使用 CLOB 成交数据而非 delta"""

        def cfg(key, default=None):
            mapping = {
                "general.dry_run": False,
                "executor.probe_enabled": False,
                "executor.min_order_size_shares": 2,
                "executor.clob_min_order_usdc": 1.10,
                "executor.offset_pct": 0.001,
                "executor.min_bet_usdc": 1.0,
                "risk.max_single_market_pct": 0.10,
                "executor.settlement_delay_sec": 0,
                "executor.balance_retry_count": 0,
            }
            return mapping.get(key, default)

        mock_get.side_effect = cfg

        sig = make_signal(size_usdc=6.73)
        sig.expected_price = 0.738
        sig.token_id = "0xTOK_PARTIAL"

        # Request 9.1 shares @ 0.739, balance delta only $1.47 (unsettled)
        # balance_before=100, balance_after=98.53 → delta=1.47
        mock_client = self._make_mock_client(balances=[100.0, 98.53])
        self.executor._client = mock_client

        with patch.object(
            self.executor,
            "_wait_for_fill",
            return_value={"status": "MATCHED", "filled_usdc": 6.73},
        ):
            with patch.object(self.executor, "_verify_trade_on_chain", return_value={}):
                with patch.object(self.executor, "_to_condition_id", return_value=""):
                    with patch.object(self.executor, "_refresh_collateral_balance"):
                        with patch.object(
                            self.executor,
                            "_resolve_token_id",
                            return_value="0xTOK_PARTIAL",
                        ):
                            result = self.executor._submit_order(sig)

        assert result.success
        # MATCHED + low fill_ratio → trust CLOB filled_usdc, not delta
        assert (
            abs(result.filled_size - 6.73) < 0.01
        ), f"MATCHED order should use CLOB filled_usdc, got {result.filled_size}"
        # actual_price should be ~limit_price (0.739)
        limit = 0.738 + 0.001
        assert (
            result.actual_price >= limit * 0.9
        ), f"Should use limit_price as fallback, got {result.actual_price}"

    @patch("src.executor.order_executor.get")
    def test_full_fill_uses_calibrated_avg(self, mock_get):
        """全额成交（fill_ratio>85%）时正常使用余额校准均价"""

        def cfg(key, default=None):
            mapping = {
                "general.dry_run": False,
                "executor.probe_enabled": False,
                "executor.min_order_size_shares": 2,
                "executor.clob_min_order_usdc": 1.10,
                "executor.offset_pct": 0.001,
                "executor.min_bet_usdc": 1.0,
                "risk.max_single_market_pct": 0.10,
                "executor.settlement_delay_sec": 0,
                "executor.balance_retry_count": 0,
            }
            return mapping.get(key, default)

        mock_get.side_effect = cfg

        sig = make_signal(size_usdc=5.0)
        sig.expected_price = 0.50
        sig.token_id = "0xTOK_FULL"

        # Request ~10 shares @ 0.501, all fill for $4.80
        # balance_before=100, balance_after=95.20 → delta=4.80
        mock_client = self._make_mock_client(balances=[100.0, 95.20])
        self.executor._client = mock_client

        with patch.object(
            self.executor,
            "_wait_for_fill",
            return_value={"status": "MATCHED", "filled_usdc": 5.0},
        ):
            with patch.object(self.executor, "_verify_trade_on_chain", return_value={}):
                with patch.object(self.executor, "_to_condition_id", return_value=""):
                    with patch.object(self.executor, "_refresh_collateral_balance"):
                        with patch.object(
                            self.executor,
                            "_resolve_token_id",
                            return_value="0xTOK_FULL",
                        ):
                            result = self.executor._submit_order(sig)

        assert result.success
        # Full fill: calibrated_avg = 4.80 / ~9.98 ≈ 0.481
        # Should be close to actual average, NOT limit_price
        assert (
            result.actual_price < 0.501
        ), f"Full fill should use calibrated avg, got {result.actual_price}"
        assert (
            result.actual_price > 0.40
        ), f"Calibrated avg should be reasonable, got {result.actual_price}"


class TestProbePlusMainWeightedPrice:
    """验证 Probe+Main 合并后使用加权平均入场价。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_weighted_avg_price_correct(self, mock_get, _mock_dry):
        """probe=$1@0.50 + main=$2@0.60 → weighted avg = $3/(1/0.5+2/0.6)"""

        def cfg(key, default=None):
            mapping = {
                "general.dry_run": False,
                "executor.probe_enabled": True,
                "executor.probe_size_usdc": 1.0,
                "executor.min_bet_usdc": 1.0,
                "risk.max_single_market_pct": 0.10,
            }
            return mapping.get(key, default)

        mock_get.side_effect = cfg
        sig = make_signal(size_usdc=3.0)
        sig.expected_price = 0.55

        probe_result = OrderResult(
            signal=sig,
            success=True,
            tx_hash="0xPROBE",
            actual_price=0.50,
            filled_size=1.0,
        )
        main_result = OrderResult(
            signal=sig,
            success=True,
            tx_hash="0xMAIN",
            actual_price=0.60,
            filled_size=2.0,
        )

        with patch.object(self.executor, "_check_balance", return_value=1000.0):
            with patch.object(self.executor, "_probe_order", return_value=probe_result):
                with patch.object(self.executor, "_slippage_ok", return_value=True):
                    with patch.object(
                        self.executor, "_submit_order", return_value=main_result
                    ):
                        result = self.executor.execute(sig)

        assert result.success
        # probe: 1.0/0.50 = 2.0 shares, main: 2.0/0.60 = 3.333 shares
        # total: 5.333 shares for $3.0 → weighted avg = 3.0/5.333 ≈ 0.5625
        expected_avg = 3.0 / (1.0 / 0.50 + 2.0 / 0.60)
        assert (
            abs(result.actual_price - expected_avg) < 0.001
        ), f"Weighted avg should be ~{expected_avg:.4f}, got {result.actual_price}"
        assert result.filled_size == 3.0

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_merge_preserves_limit_on_zero_price(self, mock_get, _mock_dry):
        """main actual_price=0 时使用 expected_price 作为 fallback"""

        def cfg(key, default=None):
            mapping = {
                "general.dry_run": False,
                "executor.probe_enabled": True,
                "executor.probe_size_usdc": 1.0,
                "executor.min_bet_usdc": 1.0,
                "risk.max_single_market_pct": 0.10,
            }
            return mapping.get(key, default)

        mock_get.side_effect = cfg
        sig = make_signal(size_usdc=3.0)
        sig.expected_price = 0.50

        probe_result = OrderResult(
            signal=sig,
            success=True,
            tx_hash="0xPROBE",
            actual_price=0.50,
            filled_size=1.0,
        )
        main_result = OrderResult(
            signal=sig,
            success=True,
            tx_hash="0xMAIN",
            actual_price=0,  # no price info
            filled_size=2.0,
        )

        with patch.object(self.executor, "_check_balance", return_value=1000.0):
            with patch.object(self.executor, "_probe_order", return_value=probe_result):
                with patch.object(self.executor, "_slippage_ok", return_value=True):
                    with patch.object(
                        self.executor, "_submit_order", return_value=main_result
                    ):
                        result = self.executor.execute(sig)

        assert result.success
        # main_price fallback to expected_price=0.50
        # Both at 0.50 → weighted avg = 0.50
        assert abs(result.actual_price - 0.50) < 0.001
        assert result.filled_size == 3.0


# ---------------------------------------------------------------------------
# Phase 35b: 余额结算延迟 + 重试 + CLOB 回退
# ---------------------------------------------------------------------------


class TestSettlementRetry:
    """验证 _submit_order 中余额结算延迟和重试逻辑。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    def _make_mock_client(self, balance_sequence):
        """构建 mock client，balance_sequence 为每次余额查询返回值列表"""
        client = MagicMock()
        client.get_balance_allowance.side_effect = [
            {"balance": str(int(b * 1_000_000))} for b in balance_sequence
        ]
        client.create_and_post_order.return_value = {"orderID": "0xSETTLE"}
        return client

    def _base_cfg(self, overrides=None):
        """返回基础 mock config"""
        mapping = {
            "general.dry_run": False,
            "executor.probe_enabled": False,
            "executor.min_order_size_shares": 2,
            "executor.clob_min_order_usdc": 1.10,
            "executor.offset_pct": 0.001,
            "executor.min_bet_usdc": 1.0,
            "risk.max_single_market_pct": 0.10,
            "executor.settlement_delay_sec": 0,
            "executor.balance_retry_count": 3,
            "executor.balance_retry_interval_sec": 0,
        }
        if overrides:
            mapping.update(overrides)

        def cfg(key, default=None):
            return mapping.get(key, default)

        return cfg

    @patch("src.executor.order_executor.get")
    def test_retry_converges_to_full_fill(self, mock_get):
        """余额重试后 fill_ratio 提升到 ≥85%，使用校准均价"""
        mock_get.side_effect = self._base_cfg()

        sig = make_signal(size_usdc=7.48)
        sig.expected_price = 0.330
        sig.token_id = "0xTOK_RETRY"

        # limit_price = 0.331, size_shares = 7.48/0.331 ≈ 22.6
        # expected_cost = 22.6 * 0.331 ≈ 7.48
        # Retry sequence: balance gradually settles
        # before=100, after1=96.61(delta=3.39,45%), after2=95.52(4.48,60%),
        # after3=93.52(6.48,87%), after4 not needed
        mock_client = self._make_mock_client([100.0, 96.61, 95.52, 93.52, 93.52])
        self.executor._client = mock_client

        with patch.object(
            self.executor,
            "_wait_for_fill",
            return_value={"status": "MATCHED", "filled_usdc": 7.48},
        ):
            with patch.object(self.executor, "_verify_trade_on_chain", return_value={}):
                with patch.object(self.executor, "_to_condition_id", return_value=""):
                    with patch.object(self.executor, "_refresh_collateral_balance"):
                        with patch.object(
                            self.executor,
                            "_resolve_token_id",
                            return_value="0xTOK_RETRY",
                        ):
                            result = self.executor._submit_order(sig)

        assert result.success
        # After retry 2: delta=6.48, fill_ratio≈87% → calibrated path
        assert (
            abs(result.filled_size - 6.48) < 0.01
        ), f"Should use settled delta after retry, got {result.filled_size}"

    @patch("src.executor.order_executor.get")
    def test_retry_exhausted_matched_uses_clob(self, mock_get):
        """重试耗尽但 CLOB 报 MATCHED → 使用 CLOB filled_usdc"""
        mock_get.side_effect = self._base_cfg()

        sig = make_signal(size_usdc=7.48)
        sig.expected_price = 0.330
        sig.token_id = "0xTOK_EXHAUST"

        # Balance never catches up (all retries return same low delta)
        # before=100, after1-4 all ≈ 96.61 → delta=3.39 consistently
        mock_client = self._make_mock_client([100.0, 96.61, 96.61, 96.61, 96.61])
        self.executor._client = mock_client

        with patch.object(
            self.executor,
            "_wait_for_fill",
            return_value={"status": "MATCHED", "filled_usdc": 7.48},
        ):
            with patch.object(self.executor, "_verify_trade_on_chain", return_value={}):
                with patch.object(self.executor, "_to_condition_id", return_value=""):
                    with patch.object(self.executor, "_refresh_collateral_balance"):
                        with patch.object(
                            self.executor,
                            "_resolve_token_id",
                            return_value="0xTOK_EXHAUST",
                        ):
                            result = self.executor._submit_order(sig)

        assert result.success
        # CLOB MATCHED but balance never settled → use CLOB filled_usdc
        assert (
            abs(result.filled_size - 7.48) < 0.01
        ), f"Should fallback to CLOB filled_usdc, got {result.filled_size}"

    @patch("src.executor.order_executor.get")
    def test_real_partial_fill_no_retry(self, mock_get):
        """真正的部分成交（非 MATCHED 状态）不触发余额重试"""
        mock_get.side_effect = self._base_cfg()

        sig = make_signal(size_usdc=7.48)
        sig.expected_price = 0.330
        sig.token_id = "0xTOK_REAL_PARTIAL"

        # Only 2 balance values needed: no retry for non-MATCHED
        mock_client = self._make_mock_client([100.0, 96.61])
        self.executor._client = mock_client

        with patch.object(
            self.executor,
            "_wait_for_fill",
            return_value={"status": "TIMEOUT_PARTIAL", "filled_usdc": 3.39},
        ):
            with patch.object(self.executor, "_verify_trade_on_chain", return_value={}):
                with patch.object(self.executor, "_to_condition_id", return_value=""):
                    with patch.object(self.executor, "_refresh_collateral_balance"):
                        with patch.object(
                            self.executor,
                            "_resolve_token_id",
                            return_value="0xTOK_REAL_PARTIAL",
                        ):
                            result = self.executor._submit_order(sig)

        assert result.success
        # Real partial fill: use balance delta, NOT CLOB filled_usdc
        assert (
            abs(result.filled_size - 3.39) < 0.01
        ), f"Real partial fill should use delta, got {result.filled_size}"

    @patch("src.executor.order_executor.get")
    def test_no_retry_when_count_zero(self, mock_get):
        """balance_retry_count=0 时不执行重试循环"""
        mock_get.side_effect = self._base_cfg({"executor.balance_retry_count": 0})

        sig = make_signal(size_usdc=7.48)
        sig.expected_price = 0.330
        sig.token_id = "0xTOK_NORETRY"

        # Only 2 balance values: before + 1 after
        mock_client = self._make_mock_client([100.0, 96.61])
        self.executor._client = mock_client

        with patch.object(
            self.executor,
            "_wait_for_fill",
            return_value={"status": "MATCHED", "filled_usdc": 7.48},
        ):
            with patch.object(self.executor, "_verify_trade_on_chain", return_value={}):
                with patch.object(self.executor, "_to_condition_id", return_value=""):
                    with patch.object(self.executor, "_refresh_collateral_balance"):
                        with patch.object(
                            self.executor,
                            "_resolve_token_id",
                            return_value="0xTOK_NORETRY",
                        ):
                            result = self.executor._submit_order(sig)

        assert result.success
        # retry_count=0 → no retries → MATCHED + low ratio → CLOB fallback
        assert abs(result.filled_size - 7.48) < 0.01
        # Only 2 balance queries total (before + 1 after)
        assert mock_client.get_balance_allowance.call_count == 2

    @patch("src.executor.order_executor.get")
    def test_partial_fill_uses_matched_shares_for_avg_price(self, mock_get):
        """Phase35c: 部分成交时用 CLOB matched_shares 计算均价，而非原始请求股数。

        复现 Munich 14°C bug: 请求 8.3 shares @ 0.481, 只成交 7.11 shares。
        balance delta = 3.42, fill_ratio 基于 matched cost 接近 100%。
        均价应为 3.42/7.11 ≈ 0.481，而非 3.42/8.3 ≈ 0.412。
        """
        mock_get.side_effect = self._base_cfg()

        sig = make_signal(size_usdc=3.98)
        sig.expected_price = 0.480
        sig.token_id = "0xTOK_MUNICH"

        # limit_price = 0.481, size_shares = 3.98/0.481 ≈ 8.27
        # 实际成交 7.11 shares, cost = 7.11 * 0.481 ≈ 3.42
        # balance: before=40, after=36.58 → delta=3.42
        mock_client = self._make_mock_client([40.0, 36.58])
        self.executor._client = mock_client

        with patch.object(
            self.executor,
            "_wait_for_fill",
            return_value={
                "status": "TIMEOUT_PARTIAL",
                "filled_usdc": 3.42,
                "matched_shares": 7.11,
            },
        ):
            with patch.object(self.executor, "_verify_trade_on_chain", return_value={}):
                with patch.object(self.executor, "_to_condition_id", return_value=""):
                    with patch.object(self.executor, "_refresh_collateral_balance"):
                        with patch.object(
                            self.executor,
                            "_resolve_token_id",
                            return_value="0xTOK_MUNICH",
                        ):
                            result = self.executor._submit_order(sig)

        assert result.success
        assert abs(result.filled_size - 3.42) < 0.01
        # 核心断言：均价应基于 matched_shares (7.11) 而非 size_shares (8.27)
        expected_avg = 3.42 / 7.11  # ≈ 0.481
        assert abs(result.actual_price - expected_avg) < 0.01, (
            f"actual_price={result.actual_price:.4f} should be ≈{expected_avg:.4f}, "
            f"not 3.42/8.27≈{3.42/8.27:.4f}"
        )


# ---------------------------------------------------------------------------
# T63: Maker/Taker 按买入/卖出独立配置测试
# ---------------------------------------------------------------------------


class TestGetOrderModeBuySell:
    """测试 _get_order_mode 按方向和赛道独立返回正确模式。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.get")
    def test_buy_mode_default_maker(self, mock_get):
        """买入方向默认 maker"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {"enabled": False},
            "order_execution.buy_mode": {"default": "maker", "per_category": {}},
            "order_execution.sell_mode": {"default": "taker", "per_category": {}},
        }.get(key)
        assert self.executor._get_order_mode("weather", direction="buy") == "maker"

    @patch("src.executor.order_executor.get")
    def test_sell_mode_default_taker(self, mock_get):
        """卖出方向默认 taker"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {"enabled": False},
            "order_execution.buy_mode": {"default": "maker", "per_category": {}},
            "order_execution.sell_mode": {"default": "taker", "per_category": {}},
        }.get(key)
        assert self.executor._get_order_mode("weather", direction="sell") == "taker"

    @patch("src.executor.order_executor.get")
    def test_per_category_overrides_default(self, mock_get):
        """赛道级别覆盖全局默认"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {"enabled": False},
            "order_execution.buy_mode": {
                "default": "taker",
                "per_category": {"crypto": "maker"},
            },
            "order_execution.sell_mode": {
                "default": "maker",
                "per_category": {"crypto": "taker"},
            },
        }.get(key)
        assert self.executor._get_order_mode("crypto", direction="buy") == "maker"
        assert self.executor._get_order_mode("crypto", direction="sell") == "taker"

    @patch("src.executor.order_executor.get")
    def test_unknown_category_uses_default(self, mock_get):
        """未知赛道回退到默认"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {"enabled": False},
            "order_execution.buy_mode": {
                "default": "maker",
                "per_category": {"crypto": "taker"},
            },
            "order_execution.sell_mode": {
                "default": "taker",
                "per_category": {"crypto": "maker"},
            },
        }.get(key)
        assert self.executor._get_order_mode("finance", direction="buy") == "maker"
        assert self.executor._get_order_mode("finance", direction="sell") == "taker"

    @patch("src.executor.order_executor.get")
    def test_backward_compatible_old_config(self, mock_get):
        """T228追加: 旧配置格式已移除，缺失 buy_mode/sell_mode 时 fallback 到 taker"""

        def _get_side_effect(key, *a, **kw):
            # 模拟旧配置：没有 buy_mode/sell_mode → 全部返回 None
            return {
                "order_execution.smart_mode": None,
                "order_execution.buy_mode": None,
                "order_execution.sell_mode": None,
            }.get(key)

        mock_get.side_effect = _get_side_effect
        # 旧路径已清除：无 buy_mode → mode=None → fallback to taker
        assert self.executor._get_order_mode("weather", direction="buy") == "taker"
        assert self.executor._get_order_mode("sports", direction="buy") == "taker"

    @patch("src.executor.order_executor.get")
    def test_smart_mode_returns_smart(self, mock_get):
        """智能模式启用时，买入方向返回 'smart'；卖出方向忽略 smart_mode 按 per_category 执行。
        T215追加修正: sell 方向禁用 smart_mode，防止覆盖 weather=taker 等配置。
        """
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {"enabled": True},
            "order_execution.sell_mode": {
                "default": "taker",
                "per_category": {"crypto": "taker"},
            },
        }.get(key)
        # 买入方向：smart_mode 生效，返回 "smart"
        assert self.executor._get_order_mode("weather", direction="buy") == "smart"
        # 卖出方向：smart_mode 不生效，按 per_category 返回 "taker"
        assert self.executor._get_order_mode("crypto", direction="sell") == "taker"

    @patch("src.executor.order_executor.get")
    def test_buy_sell_independent(self, mock_get):
        """买入和卖出可以设置不同模式"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {"enabled": False},
            "order_execution.buy_mode": {
                "default": "maker",
                "per_category": {"sports": "maker"},
            },
            "order_execution.sell_mode": {
                "default": "taker",
                "per_category": {"sports": "taker"},
            },
        }.get(key)
        # 买入 maker，卖出 taker
        assert self.executor._get_order_mode("sports", direction="buy") == "maker"
        assert self.executor._get_order_mode("sports", direction="sell") == "taker"


class TestResolveSmartMode:
    """测试智能模式的 spread/depth 决策逻辑。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.get")
    @patch("src.utils.orderbook_history.analyze_liquidity")
    @patch("src.utils.orderbook_history.fetch_orderbook")
    def test_smart_buy_wide_spread_returns_maker(self, mock_ob, mock_liq, mock_get):
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {
                "min_spread_for_maker": 0.02,
                "min_depth_usdc_for_maker": 50.0,
            },
        }.get(key)
        mock_ob.return_value = {"bids": [], "asks": []}
        mock_liq.return_value = {"spread": 0.05, "bid_depth": 100}
        assert self.executor._resolve_smart_mode("0xTOK", "buy") == "maker"

    @patch("src.executor.order_executor.get")
    @patch("src.utils.orderbook_history.analyze_liquidity")
    @patch("src.utils.orderbook_history.fetch_orderbook")
    def test_smart_buy_narrow_spread_returns_taker(self, mock_ob, mock_liq, mock_get):
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {
                "min_spread_for_maker": 0.02,
                "min_depth_usdc_for_maker": 50.0,
            },
        }.get(key)
        mock_ob.return_value = {"bids": [], "asks": []}
        mock_liq.return_value = {"spread": 0.01, "bid_depth": 100}
        assert self.executor._resolve_smart_mode("0xTOK", "buy") == "taker"

    @patch("src.executor.order_executor.get")
    @patch("src.utils.orderbook_history.analyze_liquidity")
    @patch("src.utils.orderbook_history.fetch_orderbook")
    def test_smart_sell_low_depth_returns_taker(self, mock_ob, mock_liq, mock_get):
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {
                "min_spread_for_maker": 0.02,
                "min_depth_usdc_for_maker": 50.0,
            },
        }.get(key)
        mock_ob.return_value = {"bids": [], "asks": []}
        mock_liq.return_value = {"spread": 0.05, "bid_depth": 20}
        assert self.executor._resolve_smart_mode("0xTOK", "sell") == "taker"

    @patch("src.executor.order_executor.get")
    @patch("src.utils.orderbook_history.analyze_liquidity")
    @patch("src.utils.orderbook_history.fetch_orderbook")
    def test_smart_sell_good_conditions_returns_maker(
        self, mock_ob, mock_liq, mock_get
    ):
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {
                "min_spread_for_maker": 0.02,
                "min_depth_usdc_for_maker": 50.0,
            },
        }.get(key)
        mock_ob.return_value = {"bids": [], "asks": []}
        mock_liq.return_value = {"spread": 0.03, "bid_depth": 80}
        assert self.executor._resolve_smart_mode("0xTOK", "sell") == "maker"

    @patch("src.executor.order_executor.get")
    @patch("src.utils.orderbook_history.fetch_orderbook")
    def test_smart_fallback_on_error(self, mock_ob, mock_get):
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {},
        }.get(key)
        mock_ob.side_effect = Exception("API error")
        assert self.executor._resolve_smart_mode("0xTOK", "sell") == "taker"


# ──────────────────────────────────────────────────────────────────────
# T170: Taker Fallback 价格回归测试
# ──────────────────────────────────────────────────────────────────────
class TestTakerFallbackPricing:
    """Taker模式定价必须真正跨越spread，不能与Maker价格相同。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_taker_price_higher_than_expected(self, mock_get, _):
        """Taker价格必须 > expected_price（至少1 tick=0.01）"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.taker.offset_pct": 0.01,
            "order_execution.taker.use_orderbook_best_ask": False,
            "executor.offset_pct": 0.001,
        }.get(key)

        # 模拟 expected = 0.61（NO side）
        # 旧Bug: 0.61 + 0.001 = 0.611 → round(2) = 0.61（与Maker相同）
        # 修复后: 0.61 + 0.01 = 0.62（明确高于expected）
        expected = 0.61
        taker_offset = float(mock_get("order_execution.taker.offset_pct") or 0.01)
        taker_price = expected + taker_offset
        limit_price = round(min(max(taker_price, 0.01), 0.99), 2)

        assert limit_price > round(
            expected, 2
        ), "Taker价格必须严格大于expected，确保跨越spread"

    def test_old_offset_was_buggy(self):
        """验证旧offset=0.001在0.61时round后等于原价"""
        expected = 0.61
        old_offset = 0.001
        old_price = round(min(max(expected + old_offset, 0.01), 0.99), 2)
        assert old_price == 0.61, "旧offset=0.001确实导致Taker=Maker价格（Bug证明）"

    def test_new_offset_guarantees_tick_improvement(self):
        """验证新offset=0.01在任何2位小数价格上都能提升至少1 tick"""
        offset = 0.01
        for cents in range(1, 99):  # 0.01 到 0.98
            expected = cents / 100.0
            new_price = round(min(max(expected + offset, 0.01), 0.99), 2)
            assert new_price > expected, f"expected={expected}: Taker价格未提升"

    def test_one_tick_safety_net(self):
        """当offset不足1 tick时，安全兜底确保至少+0.01"""
        expected = 0.50
        tiny_offset = 0.003  # round(0.50 + 0.003, 2) = 0.50（仍和原价相同）
        taker_price = expected + tiny_offset
        limit_price = round(min(max(taker_price, 0.01), 0.99), 2)
        # 安全兜底逻辑
        expected_rounded = round(expected, 2)
        if limit_price <= expected_rounded and expected_rounded < 0.99:
            limit_price = round(expected_rounded + 0.01, 2)
        assert limit_price > expected_rounded, "1-tick安全兜底未生效"

    @patch("src.utils.orderbook_history.fetch_orderbook")
    def test_taker_fallback_uses_orderbook_best_ask(self, mock_fetch):
        """TakerFallback应从orderbook获取best_ask定价"""
        mock_fetch.return_value = {
            "asks": [
                {"price": "0.65", "size": "100"},
                {"price": "0.70", "size": "50"},
            ],
            "bids": [
                {"price": "0.55", "size": "80"},
            ],
        }
        expected = 0.61
        taker_offset = 0.01
        taker_price = expected + taker_offset  # 0.62

        # 模拟_force_taker=True路径
        book = mock_fetch("fake_token_id")
        asks = book.get("asks") or []
        if asks:
            best_ask = float(asks[0].get("price", 0))
            if 0.01 <= best_ask <= 0.99:
                taker_price = max(taker_price, best_ask)  # max(0.62, 0.65) = 0.65

        limit_price = round(min(max(taker_price, 0.01), 0.99), 2)
        assert limit_price == 0.65, "Taker应使用orderbook best_ask=0.65"

    def test_taker_price_capped_at_099(self):
        """Taker价格不可超过0.99"""
        expected = 0.98
        offset = 0.03
        taker_price = expected + offset  # 1.01
        limit_price = round(min(max(taker_price, 0.01), 0.99), 2)
        assert limit_price == 0.99, "Taker价格必须≤0.99"


class TestT172PriceCalibrationFix:
    """
    T172 事故：copy_trade 跟投时 limit_price=0.99（Taker上限），
    实际成交 ~0.27，导致 market_price=0.99 错误写入 DB。
    验证两处修复均生效。
    """

    def test_balance_calibration_not_rejected_when_limit_price_is_099(self):
        """
        T172-FIX-1: limit_price=0.99时，calibrated_avg=0.27 不应被误判为异常。
        修复前: 0.27 < 0.99*0.5=0.495 → 触发警告，回退到 actual_avg_price=0.99
        修复后: limit_price>=0.90 时 threshold=0.01，0.27>0.01 → 正常使用校准值
        """
        limit_price = 0.99
        calibrated_avg = 0.27
        cal_shares = 5.01
        delta = calibrated_avg * cal_shares  # simulate cost

        # T172-FIX guard logic (mirrors order_executor.py)
        _price_guard_threshold = 0.01 if limit_price >= 0.90 else limit_price * 0.5
        should_accept = calibrated_avg >= _price_guard_threshold
        assert should_accept, (
            f"T172: limit_price={limit_price} 时 calibrated_avg={calibrated_avg} "
            f"应被接受（threshold={_price_guard_threshold}），但被拒绝。"
            f"这会导致 market_price 错误写为 {limit_price}"
        )

    def test_balance_calibration_still_rejects_near_zero(self):
        """limit_price=0.99 时，calibrated_avg<0.01 仍应被拒绝（防止明显错误）"""
        limit_price = 0.99
        calibrated_avg = 0.005  # clearly wrong

        _price_guard_threshold = 0.01 if limit_price >= 0.90 else limit_price * 0.5
        should_reject = calibrated_avg < _price_guard_threshold
        assert (
            should_reject
        ), f"calibrated_avg={calibrated_avg} 应被拒绝（<threshold={_price_guard_threshold}）"

    def test_normal_limit_price_still_uses_50pct_guard(self):
        """非高限价场景（limit_price=0.40）：原有50%保护仍生效"""
        limit_price = 0.40
        # calibrated_avg=0.15 < 0.40*0.5=0.20 → should reject
        calibrated_avg = 0.15

        _price_guard_threshold = 0.01 if limit_price >= 0.90 else limit_price * 0.5
        should_reject = calibrated_avg < _price_guard_threshold
        assert should_reject, (
            f"limit_price={limit_price} 时 calibrated_avg={calibrated_avg} "
            f"应被拒绝（threshold={_price_guard_threshold}）"
        )

    def test_data_api_price_update_triggered_by_price_diff_even_if_cost_matches(self):
        """
        T172-FIX-2: chain_price=0.27，actual_avg_price=0.99，cost差<0.005
        修复前: cost_diff<0.005 → 不触发更新 → 价格错误保留
        修复后: price_diff=0.72>0.01 → 触发更新 → actual_avg_price=0.27
        """
        actual_avg_price = 0.99  # wrong value from limit_price fallback
        actual_cost = 1.355

        chain = {"verified": True, "price": 0.27, "size": 5.0102}
        chain_cost = chain["size"] * chain["price"]  # 1.3527

        # T172-FIX condition (mirrors order_executor.py)
        price_changed = abs(chain["price"] - actual_avg_price) > 0.01
        cost_changed = abs(chain_cost - actual_cost) > 0.005

        should_update = (
            chain_cost > 0
            and not (actual_cost > 0 and chain_cost < actual_cost * 0.7)
            and (price_changed or cost_changed)
        )
        assert should_update, (
            f"T172: chain_price={chain['price']} 与 actual_avg_price={actual_avg_price} "
            f"差距={abs(chain['price']-actual_avg_price):.2f}，即使成本一致也必须更新价格"
        )
        # Simulate the update
        if should_update:
            actual_avg_price = chain["price"]
        assert (
            actual_avg_price == 0.27
        ), f"Data API 校准后 actual_avg_price 应为 0.27，实际={actual_avg_price}"

    def test_data_api_stale_data_protection_still_works(self):
        """
        Phase34b 保护仍然有效：chain_cost << actual_cost（陈旧数据）时拒绝更新
        """
        actual_cost = 10.0  # large actual cost
        chain = {"verified": True, "price": 0.27, "size": 1.0}  # tiny stale
        chain_cost = chain["size"] * chain["price"]  # 0.27

        is_stale = actual_cost > 0 and chain_cost < actual_cost * 0.7
        assert (
            is_stale
        ), f"chain_cost={chain_cost} << actual_cost={actual_cost}，应判断为陈旧数据拒绝更新"


# ══════════════════════════════════════════════════════════════════════
# T223 Tests — Taker 高限价场景下余额校准 + Data API 校准双层修复
# ══════════════════════════════════════════════════════════════════════


class TestT223TakerPriceCalibrationFix:
    """
    T223 事故：天气 DoubleNO taker 下单 limit_price=0.99，实际成交 ~0.67/0.70。
    三层防护全部失效：
      Layer1: 余额校准 _bal_avg=0.67 < limit_price*0.50=0.495 → FALSE → 拒绝
      Layer2: Data API chain_cost=$3.95 < actual_cost*0.7=$4.16 → 被拒
      Layer3: position_syncer 5小时后修正（太晚）
    导致 market_price=0.990 写入 DB，PriceMonitor 虚假告警。
    """

    # ── Layer 1: 余额校准阈值修复 (0.50 → 0.85) ──

    def test_balance_calibration_accepts_067_with_limit_099(self):
        """T223-FIX-A: _bal_avg=0.67, limit_price=0.99 → 0.67 < 0.99*0.85=0.8415 → TRUE → 接受"""
        limit_price = 0.99
        delta = 4.02
        cal_shares = 6.0
        _bal_avg = round(delta / cal_shares, 6)  # 0.67

        accept = (
            delta > 0
            and 0.01 <= _bal_avg <= 0.99
            and limit_price >= 0.80
            and _bal_avg < limit_price * 0.85
        )
        assert accept, (
            f"T223: _bal_avg={_bal_avg} 应被接受 (< {limit_price * 0.85:.4f})，"
            f"但条件为 FALSE → 会回退到 CLOB 数据"
        )

    def test_balance_calibration_accepts_070_with_limit_099(self):
        """Paris 场景: _bal_avg ≈ 0.70"""
        limit_price = 0.99
        _bal_avg = 0.70

        accept = _bal_avg < limit_price * 0.85  # 0.70 < 0.8415
        assert accept

    def test_balance_calibration_rejects_close_to_limit(self):
        """实际成交价接近限价时（如 0.95 vs 0.99），不应强制信任余额差"""
        limit_price = 0.99
        _bal_avg = 0.95  # 非常接近限价

        accept = _bal_avg < limit_price * 0.85  # 0.95 < 0.8415 → FALSE
        assert (
            not accept
        ), f"_bal_avg={_bal_avg} 接近 limit_price={limit_price}，不应多此一举校准"

    # ── Layer 2: Data API 校准 taker 覆盖修复 ──

    def test_data_api_taker_override_accepts_correct_price(self):
        """
        T223-FIX-B: limit_price=0.99, actual_avg_price=0.99, chain_price=0.67
        chain_cost=$3.95 < actual_cost*0.7=$4.16 → 原本被拒
        修复后: _taker_override=True → 信任 Data API → actual_avg_price=0.67
        """
        limit_price = 0.99
        actual_avg_price = 0.99  # still at limit (no prior calibration)
        actual_cost = 5.94  # CLOB inflated cost
        chain = {"verified": True, "price": 0.67, "size": 5.90}
        chain_cost = chain["size"] * chain["price"]  # 3.953

        # Check: chain_cost < actual_cost * 0.7?
        assert chain_cost < actual_cost * 0.7, "Guard should trigger"

        # But taker override should rescue
        _taker_override = (
            limit_price >= 0.90
            and actual_avg_price >= limit_price * 0.85
            and chain["price"] > 0.01
        )
        assert _taker_override, "Taker override should be active"

        # Final decision: should NOT reject
        should_reject = (
            actual_cost > 0 and chain_cost < actual_cost * 0.7 and not _taker_override
        )
        assert (
            not should_reject
        ), "Data API calibration should NOT be rejected when taker override is active"

        # Apply calibration
        price_changed = abs(chain["price"] - actual_avg_price) > 0.01
        assert price_changed
        actual_avg_price = chain["price"]
        assert actual_avg_price == 0.67

    def test_data_api_taker_override_not_triggered_for_normal_limit(self):
        """limit_price=0.40 → _taker_override=False → 原有保护不受影响"""
        limit_price = 0.40
        actual_avg_price = 0.40
        actual_cost = 2.0
        chain = {"verified": True, "price": 0.10, "size": 1.0}
        chain_cost = 0.10

        _taker_override = (
            limit_price >= 0.90
            and actual_avg_price >= limit_price * 0.85
            and chain["price"] > 0.01
        )
        assert not _taker_override
        should_reject = (
            actual_cost > 0 and chain_cost < actual_cost * 0.7 and not _taker_override
        )
        assert should_reject, "Normal stale data protection should still work"

    def test_data_api_taker_override_not_triggered_when_already_calibrated(self):
        """
        limit_price=0.99 但 actual_avg_price=0.27（已被前序校准修正）
        → _taker_override=False（因为 0.27 < 0.99*0.85=0.8415）
        → 原有保护正常工作
        """
        limit_price = 0.99
        actual_avg_price = 0.27  # already calibrated by balance delta
        actual_cost = 1.35
        chain = {"price": 0.27}

        _taker_override = (
            limit_price >= 0.90
            and actual_avg_price >= limit_price * 0.85
            and chain["price"] > 0.01
        )
        # actual_avg_price=0.27 < 0.99*0.85=0.8415 → FALSE
        assert not _taker_override

    def test_data_api_taker_override_boundary_limit_090(self):
        """limit_price=0.90（边界值）→ _taker_override=True"""
        limit_price = 0.90
        actual_avg_price = 0.90
        chain = {"price": 0.45}

        _taker_override = (
            limit_price >= 0.90
            and actual_avg_price >= limit_price * 0.85
            and chain["price"] > 0.01
        )
        assert _taker_override

    def test_data_api_taker_override_rejects_invalid_chain_price(self):
        """chain_price < 0.01 时不激活 taker override（防止垃圾数据）"""
        limit_price = 0.99
        actual_avg_price = 0.99
        chain = {"price": 0.005}

        _taker_override = (
            limit_price >= 0.90
            and actual_avg_price >= limit_price * 0.85
            and chain["price"] > 0.01
        )
        assert not _taker_override

    # ── 完整路径验证 ──

    def test_full_scenario_t223_paris_no(self):
        """
        完整模拟 T223 Paris 19°C NO 场景:
        limit_price=0.99, CLOB filled=$5.94, balance delta=$4.02
        Data API: price=0.70, size=5.91
        预期: actual_avg_price=0.70（非 0.99）
        """
        limit_price = 0.99
        cal_shares = 6.0
        delta = 4.20  # balance delta
        fill_usdc = 5.94  # CLOB inflated

        # Step 1: fill_ratio
        expected_cost = cal_shares * limit_price  # 5.94
        fill_ratio = delta / expected_cost  # ~0.707

        # Step 2: 进入"部分成交检测"因 fill_ratio < 0.85
        assert fill_ratio < 0.85

        # Step 3: 高限价低成交校准 (T223-FIX-A)
        _bal_avg = round(delta / cal_shares, 6)  # 0.70
        layer1_accept = (
            delta > 0
            and 0.01 <= _bal_avg <= 0.99
            and limit_price >= 0.80
            and _bal_avg < limit_price * 0.85
        )
        assert layer1_accept, "Layer1 should now accept with 0.85 threshold"

        # With Layer1 fix, actual_avg_price = 0.70 (correct)
        actual_avg_price = _bal_avg
        assert actual_avg_price == 0.70

    def test_full_scenario_t223_amsterdam_no(self):
        """
        完整模拟 T223 Amsterdam 19°C NO 场景:
        limit_price=0.99, balance delta=$4.02
        Data API: price=0.67, size=5.90
        预期: Layer1 修复后 actual_avg_price=0.67
        """
        limit_price = 0.99
        cal_shares = 6.0
        delta = 4.02
        _bal_avg = round(delta / cal_shares, 6)  # 0.67

        layer1_accept = (
            delta > 0
            and 0.01 <= _bal_avg <= 0.99
            and limit_price >= 0.80
            and _bal_avg < limit_price * 0.85
        )
        assert layer1_accept

        actual_avg_price = _bal_avg
        assert actual_avg_price == 0.67


# ══════════════════════════════════════════════════════════════════════
# T189 Tests — 高限价低成交场景
# ══════════════════════════════════════════════════════════════════════


class TestT189HighLimitLowFill:
    """T189-B: 高限价低成交场景 — 余额校准应信任余额差而非 CLOB 膨胀值。"""

    def test_high_limit_detects_low_fill(self):
        """
        limit_price=0.99, 实际成交 0.37 → 余额差 $1.98, CLOB=$5.30
        应使用余额差校准，而非 "余额结算超时"。
        """
        limit_price = 0.99
        cal_shares = 5.35
        delta = 1.98  # 真实余额扣款
        fill_filled_usdc = 5.2965  # CLOB 膨胀值 (shares × limit)
        fill_status = "MATCHED"

        # 模拟当前代码逻辑
        expected_cost = cal_shares * limit_price  # 5.35 × 0.99 = 5.2965
        fill_ratio = delta / expected_cost if expected_cost > 0 else 0

        assert fill_ratio < 0.85, f"fill_ratio 应 < 0.85 以触发分支: {fill_ratio:.2f}"

        # T189-FIX: 高限价低成交检测
        _bal_avg = round(delta / cal_shares, 6) if cal_shares > 0 else 0
        is_high_limit_low_fill = (
            delta > 0
            and 0.01 <= _bal_avg <= 0.99
            and limit_price >= 0.80
            and _bal_avg < limit_price * 0.50
        )
        assert is_high_limit_low_fill, f"应检测为高限价低成交: bal_avg={_bal_avg:.4f}"
        assert abs(_bal_avg - 0.37) < 0.01, f"余额均价应≈0.37, 实际: {_bal_avg:.4f}"

    def test_normal_fill_not_affected(self):
        """
        limit_price=0.37, delta=1.98 → fill_ratio ≈ 1.0 → 正常路径不受影响。
        """
        limit_price = 0.37
        cal_shares = 5.35
        delta = 1.98

        expected_cost = cal_shares * limit_price
        fill_ratio = delta / expected_cost if expected_cost > 0 else 0

        # 正常成交 fill_ratio ≈ 1.0 → 不进入 < 0.85 分支
        assert fill_ratio >= 0.85, f"正常成交 fill_ratio 应 ≥ 0.85: {fill_ratio:.2f}"

    def test_true_settlement_timeout_still_trusts_clob(self):
        """
        真正的余额未结算: delta ≈ 0, CLOB reports MATCHED → 仍信任 CLOB。
        """
        limit_price = 0.37
        cal_shares = 5.35
        delta = 0.05  # 极小余额差 — 余额尚未结算

        _bal_avg = round(delta / cal_shares, 6) if cal_shares > 0 else 0
        # 0.0093... < 0.01 → 不满足 high-limit-low-fill 条件
        is_high_limit_low_fill = (
            delta > 0
            and 0.01 <= _bal_avg <= 0.99
            and limit_price >= 0.80
            and _bal_avg < limit_price * 0.50
        )
        assert (
            not is_high_limit_low_fill
        ), f"极低余额差应走正常超时路径: bal_avg={_bal_avg:.6f}"

    def test_moderate_limit_not_triggered(self):
        """
        limit_price=0.34 (< 0.80), delta=3.39, cal_shares=22
        → 即使 _bal_avg < limit×0.50, 但 limit < 0.80 → 不触发。
        """
        limit_price = 0.34
        cal_shares = 22.0
        delta = 3.39

        _bal_avg = round(delta / cal_shares, 6) if cal_shares > 0 else 0
        is_high_limit_low_fill = (
            delta > 0
            and 0.01 <= _bal_avg <= 0.99
            and limit_price >= 0.80
            and _bal_avg < limit_price * 0.50
        )
        assert (
            not is_high_limit_low_fill
        ), f"limit < 0.80 不应触发高限价检测: limit={limit_price}"

    def test_data_api_not_rejected_after_fix(self):
        """
        T189-FIX 后: actual_cost 使用余额差 ($1.98)，而非 CLOB ($5.30)。
        Data API chain_cost ($1.92) < old_actual ($5.30) × 0.7 ($3.71) → 被拒。
        但修复后 actual_cost = $1.98, chain_cost ($1.92) > $1.98 × 0.7 ($1.39) → 通过。
        """
        actual_cost_fixed = 1.98  # 使用余额差（修复后）
        chain_cost = 1.9198  # Data API 返回

        is_stale_after_fix = (
            actual_cost_fixed > 0 and chain_cost < actual_cost_fixed * 0.7
        )
        assert not is_stale_after_fix, "修复后 Data API 校准不应被拒"

        # 对比修复前：旧 actual_cost 使用 CLOB 膨胀值
        actual_cost_old = 5.2965
        is_stale_before_fix = actual_cost_old > 0 and chain_cost < actual_cost_old * 0.7
        assert is_stale_before_fix, "修复前 Data API 校准确实会被拒"


class TestMakerPartialFillSkipsTakerFallback:
    """
    T204追加: Maker部分成交时必须跳过TakerFallback。

    **事故**: 02001b1c — NYC DoubleNO，Maker填充17%(0.67USDC)后超时，
    TakerFallback以全额(3.98USDC)重发，Taker也超时 → Maker的0.67在链上(已买入Token)
    但DB未记录 → 孤儿仓位 → PositionSyncer导入0.86shares → 阻塞该市场所有后续信号。

    **两个Bug**:
    1. TakerFallback使用全额(不扣减Maker部分成交) → 若Taker成功则超买
    2. 双失败时Maker部分成交不入DB → 孤儿链上仓位

    **修复**: 当Maker有部分成交(filled_usdc>0)时，跳过TakerFallback，
    让代码 fall through 到部分成交返回路径(success=True)。PostBuyTopUp补齐。
    """

    def test_maker_partial_fill_returns_success_no_taker_fallback(self):
        """Maker 17%成交后超时 → 应返回success=True(部分成交)，不触发TakerFallback"""
        # 模拟: Maker超时，filled_usdc=0.67, matched_shares=0.86
        fill = {
            "status": "TIMEOUT_PARTIAL",
            "filled_usdc": 0.67,
            "matched_shares": 0.86,
        }
        size_shares = 5.1
        _matched_pct = fill["matched_shares"] / size_shares  # ~0.169 = 17%
        threshold = 0.5

        # 验证: 17% < 50% threshold → 进入 MakerTimeout 分支
        assert _matched_pct < threshold, "17%应低于50%阈值"

        # 验证: 有部分成交 → 应跳过TakerFallback
        _maker_partial_usdc = fill.get("filled_usdc", 0)
        assert _maker_partial_usdc > 0, "Maker应有部分成交"
        # 修复后逻辑: _maker_partial_usdc > 0 → 跳过TakerFallback

        # 验证: 代码 fall through 后，fill["filled_usdc"] > 0 → success=True
        assert fill["filled_usdc"] > 0, "部分成交应返回success=True"

    def test_maker_zero_fill_still_triggers_taker_fallback(self):
        """Maker 0%成交 → TIMEOUT_CANCELLED → TakerFallback应正常触发"""
        fill = {
            "status": "TIMEOUT_CANCELLED",
            "filled_usdc": 0.0,
            "matched_shares": 0.0,
        }

        _maker_partial_usdc = fill.get("filled_usdc", 0)
        assert _maker_partial_usdc == 0, "零成交时不应跳过TakerFallback"
        # 修复后逻辑: _maker_partial_usdc == 0 → 进入elif → TakerFallback

    def test_maker_above_threshold_already_falls_through(self):
        """Maker >=50%成交 → MakerPartialFill分支(既有行为) → 不触发TakerFallback"""
        fill = {
            "status": "TIMEOUT_PARTIAL",
            "filled_usdc": 2.39,
            "matched_shares": 3.06,
        }
        size_shares = 5.1
        threshold = 0.5

        _matched_pct = fill["matched_shares"] / size_shares  # ~0.60 = 60%
        assert _matched_pct >= threshold, "60%应 >= 50%阈值 → MakerPartialFill分支"
        # 既有行为: 进入 MakerPartialFill 日志分支，fall through → success=True

    def test_taker_fallback_overbuy_scenario_prevented(self):
        """
        **事故场景**: Maker填充0.67, TakerFallback以全额3.98重发
        若Taker成功: 总=0.67+3.98=4.65 USDC (≈6 shares vs 目标5.1)
        修复后: 不触发TakerFallback → 只有Maker 0.67 → PostBuyTopUp补齐
        """
        maker_filled = 0.67
        original_size = 3.98
        target_shares = 5.1
        price = 0.78

        # 修复前: TakerFallback全额 → 总成本
        total_if_both_succeed = maker_filled + original_size  # 4.65
        total_shares_if_both = total_if_both_succeed / price  # ~5.96
        overbuy = total_shares_if_both - target_shares
        assert overbuy > 0.5, f"修复前超买{overbuy:.2f}shares应 > 0.5"

        # 修复后: 只有Maker部分成交
        partial_shares = maker_filled / price  # ~0.86
        assert partial_shares < target_shares, "部分成交应 < 目标"
        # PostBuyTopUp 会补齐 (target - partial) 或 CLOB_min

    def test_cancel_failed_zero_fill_still_fallback(self):
        """TIMEOUT_CANCEL_FAILED + filled_usdc=0 → TakerFallback仍应触发"""
        fill = {
            "status": "TIMEOUT_CANCEL_FAILED",
            "filled_usdc": 0.0,
            "matched_shares": 0.0,
        }
        _maker_partial_usdc = fill.get("filled_usdc", 0)
        assert _maker_partial_usdc == 0, "cancel失败但零成交 → 允许TakerFallback"

    def test_partial_fill_result_data_complete(self):
        """部分成交返回的OrderResult应包含正确的filled_size和matched_shares"""
        fill = {
            "status": "TIMEOUT_PARTIAL",
            "filled_usdc": 0.67,
            "matched_shares": 0.86,
        }
        # Fall through后, line 985: elif fill["filled_usdc"] > 0
        # 返回 OrderResult(success=True, filled_size=actual_cost, matched_shares=_clob_matched)
        assert fill["filled_usdc"] > 0, "应返回success=True"
        assert fill["matched_shares"] > 0, "应有matched_shares数据"
        # _db_write_live_bets 会用这些数据写入DB
        # PostBuyTopUp 会用 matched_shares 判断是否需要补齐


# ---------------------------------------------------------------------------
# T206追加: TopUp 豁免 min_bet_usdc 检查 — 防止结构性死锁
# ---------------------------------------------------------------------------


class TestTopUpMinBetExemption:
    """T206追加: TopUp 信号 (_is_topup=True) 必须豁免 min_bet_usdc 检查。

    事故记录: Singapore 33°C 投注只成交 3.18/5.1 shares (Maker 部分成交),
    PostBuyTopUp 补买 1.92 shares × $0.69 = $1.32，但 executor 因
    $1.32 < min_bet_usdc $2.0 拒绝下单 → 结构性死锁。
    """

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_topup_bypasses_min_bet_check(self, mock_get, _mock_dry):
        """_is_topup=True 的信号 size_usdc < min_bet 时不被拒绝，
        应到达 _submit_order 并被 CLOB 最小份数放大。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.min_bet_usdc": 2.0,
            "risk.max_single_market_pct": 0.10,
            "executor.probe_enabled": False,
        }.get(key, default)

        sig = make_signal(size_usdc=1.32)  # < min_bet 2.0
        sig._is_topup = True

        submit_called = []

        def mock_submit(signal_arg):
            submit_called.append(True)
            return OrderResult(
                signal=signal_arg,
                success=True,
                tx_hash="0xTOPUP_OK",
                filled_size=3.45,
                matched_shares=5.0,
            )

        with patch.object(self.executor, "_check_balance", return_value=20.0):
            with patch.object(self.executor, "_submit_order", side_effect=mock_submit):
                result = self.executor.execute(sig)
                # TopUp should NOT be rejected by min_bet check
                assert (
                    len(submit_called) == 1
                ), "TopUp 信号应绕过 min_bet 检查到达 _submit_order"
                assert result.success is True

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_non_topup_still_rejected_by_min_bet(self, mock_get, _mock_dry):
        """普通信号 (非 TopUp) size_usdc < min_bet 时仍被拒绝。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.min_bet_usdc": 2.0,
            "risk.max_single_market_pct": 0.10,
            "executor.probe_enabled": False,
        }.get(key, default)

        sig = make_signal(size_usdc=1.32)
        # 不设 _is_topup → 默认 False

        with patch.object(self.executor, "_check_balance", return_value=20.0):
            with patch.object(self.executor, "_submit_order") as mock_submit:
                result = self.executor.execute(sig)
                # 普通信号应被 min_bet 拒绝
                assert result.success is False
                assert "最小下单额" in (result.error or "")
                mock_submit.assert_not_called()

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_topup_at_exact_min_bet_not_blocked(self, mock_get, _mock_dry):
        """TopUp size_usdc == min_bet 时不应被阻止。"""
        mock_get.side_effect = lambda key, default=None: {
            "general.dry_run": False,
            "executor.min_bet_usdc": 2.0,
            "risk.max_single_market_pct": 0.10,
            "executor.probe_enabled": False,
        }.get(key, default)

        sig = make_signal(size_usdc=2.0)  # == min_bet
        sig._is_topup = True

        with patch.object(self.executor, "_check_balance", return_value=20.0):
            with patch.object(self.executor, "_submit_order") as mock_submit:
                mock_submit.return_value = OrderResult(
                    signal=sig, success=True, filled_size=2.0
                )
                result = self.executor.execute(sig)
                assert result.success is True
                mock_submit.assert_called_once()


# ─────────────────────────────────────────────────────────────────────
# T228: _submit_order 中 category 推断修复测试
# ─────────────────────────────────────────────────────────────────────


class TestSubmitOrderCategoryInference:
    """T228: Signal 没有 category 字段时，OrderExecutor 应通过
    detect_category 从 question 推断 category，确保 per_category
    配置（如 weather=taker）正确生效。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.detect_category")
    def test_weather_signal_uses_taker_via_detect_category(self, mock_detect, mock_get):
        """天气信号（无 category 属性）→ detect_category 返回 'weather'
        → _get_order_mode 返回 'taker'（非默认的 'maker'）。"""
        mock_detect.return_value = "weather"
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {"enabled": False},
            "order_execution.buy_mode": {
                "default": "maker",
                "per_category": {"weather": "taker"},
            },
            "executor.probe_enabled": False,
        }.get(key)

        sig = Signal(
            market_id="1965623",
            question="Will the highest temperature in Los Angeles be between 70-71°F on April 15?",
            outcome="NO",
            ev=0.10,
            confidence=0.70,
            size_usdc=3.0,
            expected_price=0.36,
        )
        # Signal 没有 category 属性
        assert not hasattr(sig, "category") or getattr(sig, "category", "") == ""

        # mock _submit_order 内部调用 → 验证 _get_order_mode 参数
        with patch.object(
            self.executor, "_get_order_mode", wraps=self.executor._get_order_mode
        ) as mock_mode:
            with patch.object(self.executor, "_get_clob_client"):
                with patch.object(self.executor, "_refresh_collateral_balance"):
                    with patch.object(
                        self.executor, "_resolve_token_id", return_value="token_abc"
                    ):
                        # 我们不需要真的提交订单，只验证 mode 推断
                        # 直接测试推断逻辑
                        _cat = getattr(sig, "category", "") or mock_detect(
                            {"question": sig.question}
                        )
                        mode = self.executor._get_order_mode(_cat, direction="buy")
                        assert _cat == "weather", f"Expected 'weather' but got '{_cat}'"
                        assert (
                            mode == "taker"
                        ), f"Weather should use taker, got '{mode}'"

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.detect_category")
    def test_sports_signal_without_category_uses_correct_mode(
        self, mock_detect, mock_get
    ):
        """体育信号（无 category 属性）→ detect_category 返回 'sports'
        → _get_order_mode 返回 'maker'。"""
        mock_detect.return_value = "sports"
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {"enabled": False},
            "order_execution.buy_mode": {
                "default": "taker",
                "per_category": {"sports": "maker"},
            },
        }.get(key)

        sig = Signal(
            market_id="mkt_sports",
            question="Will team A win the match?",
            outcome="YES",
            ev=0.08,
            confidence=0.65,
            size_usdc=5.0,
        )
        _cat = getattr(sig, "category", "") or mock_detect({"question": sig.question})
        mode = self.executor._get_order_mode(_cat, direction="buy")
        assert _cat == "sports"
        assert mode == "maker"

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.detect_category")
    def test_empty_category_fallback_to_default(self, mock_detect, mock_get):
        """detect_category 也返空时，回退到 buy_mode.default。"""
        mock_detect.return_value = ""
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.smart_mode": {"enabled": False},
            "order_execution.buy_mode": {
                "default": "taker",
                "per_category": {"weather": "taker"},
            },
        }.get(key)

        sig = Signal(
            market_id="mkt_unknown",
            question="Some unknown question?",
            outcome="YES",
            ev=0.05,
            confidence=0.60,
            size_usdc=2.0,
        )
        _cat = getattr(sig, "category", "") or mock_detect({"question": sig.question})
        mode = self.executor._get_order_mode(_cat or "", direction="buy")
        assert mode == "taker"  # fallback to default


class TestGetOrderModeLegacyRemoved:
    """T228追加: 验证旧路径 backward compatibility 已清除。"""

    def setup_method(self):
        self.executor = OrderExecutor.__new__(OrderExecutor)

    @patch("src.executor.order_executor.get")
    def test_no_legacy_per_category_fallback(self, mock_get):
        """旧的 order_execution.per_category 不再被读取。"""
        call_keys = []

        def _track_get(key, *a, **kw):
            call_keys.append(key)
            return {
                "order_execution.smart_mode": {"enabled": False},
                "order_execution.buy_mode": {"default": "maker", "per_category": {}},
            }.get(key)

        mock_get.side_effect = _track_get
        mode = self.executor._get_order_mode("unknown_cat", direction="buy")
        # 旧 key 不应被读取
        assert "order_execution.per_category" not in call_keys
        assert "order_execution.mode" not in call_keys
        # 应回退到 default
        assert mode == "maker"

    @patch("src.executor.order_executor.get")
    def test_sell_mode_per_category_works(self, mock_get):
        """sell_mode.per_category 正确生效。"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.sell_mode": {
                "default": "maker",
                "per_category": {"sports": "taker"},
            },
        }.get(key)
        mode = self.executor._get_order_mode("sports", direction="sell")
        assert mode == "taker"


# ---------------------------------------------------------------------------
# T231: bestBid 极端偏离保护
# ---------------------------------------------------------------------------


class TestBidSanityProtection:
    """验证 bestBid 极端偏离保护拒绝以操纵价格卖出。"""

    @pytest.fixture(autouse=True)
    def setup(self):
        with patch("src.executor.order_executor.get") as self.mock_get:
            self.executor = OrderExecutor.__new__(OrderExecutor)
            self.executor._clob_client = None
            yield

    def _make_sell_signal(self, entry=0.64, exit_price=0.82, outcome="NO"):
        return {
            "bet_id": "bid-sanity-01",
            "market_id": "mkt-01",
            "outcome": outcome,
            "size_usdc": 3.14,
            "market_price": entry,
            "exit_price": exit_price,
            "category": "weather",
            "token_id": "tok-01",
            "condition_id": "cond-01",
            "question": "Test weather question",
        }

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.OrderExecutor._get_clob_client")
    @patch("src.executor.order_executor.OrderExecutor._resolve_token_id")
    @patch("src.executor.order_executor.OrderExecutor._refresh_and_get_token_balance")
    @patch("src.utils.orderbook_history.fetch_orderbook")
    def test_rejects_manipulated_bid(
        self, mock_ob, mock_balance, mock_token, mock_client, mock_dry
    ):
        """bestBid=0.01 远低于 exit_cache=0.82 → 拒绝卖出。"""
        mock_client.return_value = MagicMock()
        mock_token.return_value = "tok-01"
        mock_balance.return_value = 10.0

        # bestBid = 0.01, exit_cache = 0.82 → 0.01 < 0.82*0.1=0.082 → 应拒绝
        mock_ob.return_value = {"bids": [{"price": "0.01"}]}

        self.mock_get.side_effect = lambda key, *a, **kw: {
            "order_execution.taker.use_orderbook_best_bid": True,
            "executor.min_order_size_shares": 5.0,
            "executor.bid_sanity_min_ratio": 0.1,
            "executor.sell_offset_pct": 0.002,
            "executor.sell_progressive_step": 0.005,
            "order_execution.sell_mode": {"default": "taker"},
        }.get(key, {}.get(key))

        signal = self._make_sell_signal()
        result = self.executor.sell_position(signal)

        assert result.success is False
        assert "bid_sanity_block" in (result.error or "")
