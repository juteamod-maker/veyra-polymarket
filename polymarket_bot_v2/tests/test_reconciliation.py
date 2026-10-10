"""
test_reconciliation.py — 链上对账修复的单元测试
覆盖：
  1. sell_position 余额查询顺序（先查余额再下单）
  2. actual_price 使用真实成交均价（非限价）
  3. _db_write_orders 使用 filled_size（非 sig.size_usdc）
  4. _reconcile_with_chain 链上对账逻辑
  5. PositionSyncer._reconcile_auto_positions 同步对账
"""

import sqlite3
import time
import uuid
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from src.executor.order_executor import OrderExecutor, OrderResult
from src.signal.signal_engine import Signal
from src.sync.position_syncer import PositionSyncer

# ---------------------------------------------------------------------------
# 辅助工具
# ---------------------------------------------------------------------------


def make_signal(**kwargs) -> Signal:
    defaults = dict(
        market_id="mkt_001",
        question="Will it rain?",
        outcome="YES",
        ev=0.08,
        confidence=0.75,
        size_usdc=3.0,
    )
    defaults.update(kwargs)
    return Signal(**defaults)


def _make_open_bet(
    bet_id: str = None,
    market_id: str = "mkt_001",
    question: str = "Will it rain?",
    outcome: str = "YES",
    size_usdc: float = 3.0,
    market_price: float = 0.55,
    source: str = "auto",
    condition_id: str = "0xcond_abc",
    ts_entered: float = None,
):
    return {
        "bet_id": bet_id or str(uuid.uuid4()),
        "market_id": market_id,
        "question": question,
        "outcome": outcome,
        "size_usdc": size_usdc,
        "market_price": market_price,
        "source": source,
        "condition_id": condition_id,
        "resolved_outcome": None,
        "ts_entered": ts_entered or (time.time() - 600),
    }


# ---------------------------------------------------------------------------
# 1. sell_position 余额查询顺序：先查余额再下单
# ---------------------------------------------------------------------------


class TestSellPositionBalanceOrder:
    """验证 sell_position() 在下单前查询余额。"""

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_balance_queried_before_order(self, mock_get, _mock_dry):
        """sell_balance_before 必须在 create_and_post_order 之前被调用。"""
        mock_get.side_effect = lambda k, *a: {
            "general.dry_run": False,
            "executor.sell_offset_pct": 0.002,
            "executor.sweep_min_shares": 0.01,
            "executor.fill_timeout_sec": 1,
            "executor.fill_poll_interval_sec": 0.1,
        }.get(k)

        executor = OrderExecutor()

        # Mock CLOB client
        mock_client = MagicMock()
        # Token balance refresh → return shares
        mock_client.update_balance_allowance.return_value = None

        call_log = []

        def track_balance(params):
            # Distinguish CONDITIONAL (token) vs COLLATERAL (USDC) calls
            # by checking if token_id is set on params
            token_id = getattr(params, "token_id", None)
            if token_id:
                call_log.append("get_token_balance")
                return {"balance": "5000000", "allowances": {"0xop": "1"}}
            else:
                call_log.append("get_usdc_balance")
                return {"balance": "10000000", "allowances": {}}

        def track_order(*a, **kw):
            call_log.append("create_order")
            return {"orderID": "sell_order_1"}

        mock_client.get_balance_allowance.side_effect = track_balance
        mock_client.create_and_post_order.side_effect = track_order
        # Fill check
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "5.0",
            "price": "0.45",
        }

        executor._client = mock_client

        # Mock _resolve_token_id to avoid real API call
        with patch.object(executor, "_resolve_token_id", return_value="0xtok_yes"):
            bet = {
                "market_id": "0xcond_test",
                "outcome": "YES",
                "exit_price": 0.45,
                "market_price": 0.50,
                "size_usdc": 2.50,
                "question": "Test question",
                "trigger": "profit_take",
            }
            result = executor.sell_position(bet)

        # 核心验证：USDC 余额查询应在 create_order 之前
        order_idx = None
        for i, c in enumerate(call_log):
            if c == "create_order":
                order_idx = i
                break
        assert order_idx is not None, f"create_order 未被调用！call_log: {call_log}"
        usdc_before_order = [c for c in call_log[:order_idx] if c == "get_usdc_balance"]
        assert (
            len(usdc_before_order) >= 1
        ), f"USDC 余额查询应在下单之前！调用顺序: {call_log}"


# ---------------------------------------------------------------------------
# 2. actual_price 使用真实成交均价
# ---------------------------------------------------------------------------


class TestActualPriceCalibration:
    """验证 _submit_order 返回真实均价而非限价。"""

    @patch("src.utils.config_loader.get")
    def test_actual_price_from_balance_diff(self, mock_get):
        """MATCHED + fill_ratio<85% 时，使用 CLOB 成交数据（余额尚未结算）。
        Phase 35b 修复：CLOB 报告 MATCHED 但余额差不足时信任 CLOB 数据。
        """
        mock_get.side_effect = lambda k: {
            "general.dry_run": False,
            "executor.offset_pct": 0.001,
            "executor.clob_min_order_usdc": 1.10,
            "executor.min_bet_usdc": 1.0,
            "executor.min_order_size_shares": 2,
            "executor.fill_timeout_sec": 1,
            "executor.fill_poll_interval_sec": 0.1,
            "risk.max_single_market_pct": 0.05,
            "executor.settlement_delay_sec": 0,
            "executor.balance_retry_count": 0,
        }.get(k)

        executor = OrderExecutor()
        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {"orderID": "order_123"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "4.0",
            "price": "0.501",
        }

        # 模拟余额查询：下单前 $20，下单后 $18
        # 实际花费 = $2.00，下单股数 = 3.0/0.501 ≈ 5.988
        # fill_ratio = 2.0 / (5.988 × 0.501) ≈ 0.67 → 部分成交
        balance_sequence = iter([20.0, 18.0])

        def mock_balance_allowance(params):
            bal = next(balance_sequence, 18.0)
            return {"balance": str(int(bal * 1_000_000)), "allowances": {}}

        mock_client.get_balance_allowance.side_effect = mock_balance_allowance

        mock_client.update_balance_allowance.return_value = None
        executor._client = mock_client

        sig = make_signal(size_usdc=3.0)
        sig.expected_price = 0.50
        sig.token_id = "0xtok_yes"

        result = executor._submit_order(sig)

        assert result.success is True
        # Phase 36: matched_shares 修正 fill_ratio 计算
        # balance_delta=2.0, matched_shares=4.0 → fill_ratio=2.0/(4.0×0.501)≈0.998
        # fill_ratio≥0.85 → 使用余额校准均价 = 2.0/4.0 = 0.5
        assert (
            result.actual_price == 0.5
        ), f"余额校准均价应为 0.5，got {result.actual_price}"
        # filled_size = balance_delta = 2.0
        assert (
            abs(result.filled_size - 2.0) < 0.01
        ), f"filled_size should use balance delta, got {result.filled_size}"

    @patch("src.utils.config_loader.get")
    def test_full_fill_uses_calibrated_avg(self, mock_get):
        """全额成交（fill_ratio≥85%）时，actual_price 使用余额校准的真实均价。"""
        mock_get.side_effect = lambda k: {
            "general.dry_run": False,
            "executor.offset_pct": 0.001,
            "executor.clob_min_order_usdc": 1.10,
            "executor.min_bet_usdc": 1.0,
            "executor.min_order_size_shares": 2,
            "executor.fill_timeout_sec": 1,
            "executor.fill_poll_interval_sec": 0.1,
            "risk.max_single_market_pct": 0.05,
            "executor.settlement_delay_sec": 0,
            "executor.balance_retry_count": 0,
        }.get(k)

        executor = OrderExecutor()
        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {"orderID": "order_789"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "6.0",
            "price": "0.501",
        }

        # balance_before=20, balance_after=17.1 → delta=2.9
        # size_shares = 3.0 / 0.501 ≈ 5.988
        # expected_cost = 5.988 × 0.501 = 3.0
        # fill_ratio = 2.9 / 3.0 ≈ 0.967 → 全额成交
        # calibrated_avg = 2.9 / 5.988 ≈ 0.484
        balance_sequence = iter([20.0, 17.1])

        def mock_balance_allowance(params):
            bal = next(balance_sequence, 17.1)
            return {"balance": str(int(bal * 1_000_000)), "allowances": {}}

        mock_client.get_balance_allowance.side_effect = mock_balance_allowance
        mock_client.update_balance_allowance.return_value = None
        executor._client = mock_client

        sig = make_signal(size_usdc=3.0)
        sig.expected_price = 0.50
        sig.token_id = "0xtok_yes"

        result = executor._submit_order(sig)

        assert result.success is True
        # Full fill: calibrated_avg ≈ 0.484 (got better price than limit 0.501)
        assert (
            result.actual_price < 0.501
        ), f"Full fill should use calibrated avg <limit, got {result.actual_price}"
        assert (
            result.actual_price > 0.40
        ), f"Calibrated avg should be reasonable, got {result.actual_price}"

    @patch("src.utils.config_loader.get")
    def test_actual_price_fallback_to_limit(self, mock_get):
        """当余额差校准失败时，actual_price 回退到限价。"""
        mock_get.side_effect = lambda k: {
            "general.dry_run": False,
            "executor.offset_pct": 0.001,
            "executor.clob_min_order_usdc": 1.10,
            "executor.min_bet_usdc": 1.0,
            "executor.min_order_size_shares": 2,
            "executor.fill_timeout_sec": 1,
            "executor.fill_poll_interval_sec": 0.1,
            "risk.max_single_market_pct": 0.05,
        }.get(k)

        executor = OrderExecutor()
        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {"orderID": "order_456"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "4.0",
            "price": "0.50",
        }

        mock_client.update_balance_allowance.return_value = None
        # 余额查询失败（抛异常）
        mock_client.get_balance_allowance.side_effect = Exception(
            "balance query failed"
        )

        executor._client = mock_client

        sig = make_signal(size_usdc=3.0)
        sig.expected_price = 0.50
        sig.token_id = "0xtok_yes"

        result = executor._submit_order(sig)

        assert result.success is True
        # 余额查询全部失败时，回退到限价
        # T289: tick size = 0.01, round to 2 decimals
        expected_limit = round(0.50 + 0.001, 2)
        assert result.actual_price == expected_limit


# ---------------------------------------------------------------------------
# 3. _db_write_orders 使用 filled_size
# ---------------------------------------------------------------------------


class TestDbWriteOrders:
    """验证 _db_write_orders 优先使用 filled_size 而非 sig.size_usdc。"""

    def test_orders_use_filled_size(self):
        """当 filled_size > 0 时，orders 表应记录 filled_size。"""
        from main import _db_write_orders

        sig = make_signal(size_usdc=5.0)
        result = OrderResult(
            signal=sig,
            success=True,
            tx_hash="order_789",
            actual_price=0.55,
            filled_size=4.80,
        )

        with patch("main.connect") as mock_conn:
            ctx = mock_conn.return_value.__enter__.return_value
            _db_write_orders([result])

            # 验证 executemany 被调用
            assert ctx.executemany.called
            insert_rows = ctx.executemany.call_args[0][1]
            assert len(insert_rows) == 1
            # rows[0] 的第4个元素是 size_usdc
            recorded_size = insert_rows[0][3]
            assert (
                recorded_size == 4.80
            ), f"应记录 filled_size=4.80，实际记录 {recorded_size}"

    def test_orders_fallback_to_sig_size(self):
        """当 filled_size=0 时，回退到 sig.size_usdc。"""
        from main import _db_write_orders

        sig = make_signal(size_usdc=5.0)
        result = OrderResult(
            signal=sig,
            success=False,
            tx_hash="",
            actual_price=0.0,
            filled_size=0.0,
            error="failed",
        )

        with patch("main.connect") as mock_conn:
            ctx = mock_conn.return_value.__enter__.return_value
            _db_write_orders([result])

            insert_rows = ctx.executemany.call_args[0][1]
            recorded_size = insert_rows[0][3]
            assert recorded_size == 5.0


# ---------------------------------------------------------------------------
# 4. _reconcile_with_chain 链上对账
# ---------------------------------------------------------------------------


class TestReconcileWithChain:
    """验证 _reconcile_with_chain 对账逻辑。"""

    @patch("main.get")
    def test_skips_dry_run(self, mock_get):
        """DRY RUN 模式下不执行对账。"""
        from main import _reconcile_with_chain

        mock_get.return_value = True  # dry_run=True
        # 不应有任何 API 调用
        with patch("main.requests.get") as mock_req:
            _reconcile_with_chain()
            mock_req.assert_not_called()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.connect")
    @patch("main.requests.get")
    @patch("main.get")
    def test_corrects_auto_position(
        self, mock_cfg, mock_req, mock_conn, mock_open_bets, _mock_dry
    ):
        """auto 来源仓位的 size_usdc 和 market_price 与链上不一致时应更新。"""
        from main import _reconcile_with_chain

        mock_cfg.side_effect = lambda k: {
            "general.dry_run": False,
        }.get(k)

        # 链上数据
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "conditionId": "0xcond_1",
                "size": "10.0",
                "initialValue": "5.50",
                "avgPrice": "0.55",
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        # DB 数据：有差异
        mock_open_bets.return_value = [
            _make_open_bet(
                bet_id="bet_001",
                size_usdc=5.20,  # 链上 5.50，差 0.30
                market_price=0.58,  # 链上 0.55，差 0.03
                source="auto",
                condition_id="0xcond_1",
            )
        ]

        ctx = mock_conn.return_value.__enter__.return_value
        _reconcile_with_chain()

        # 验证 UPDATE 被调用（可能含 scale-in INSERT + calibration UPDATE）
        assert ctx.execute.call_count >= 1
        # 取包含 UPDATE paper_bets 的调用
        update_calls = [
            c
            for c in ctx.execute.call_args_list
            if len(c[0]) >= 1 and "UPDATE paper_bets" in str(c[0][0])
        ]
        assert len(update_calls) >= 1, "应至少有一次 UPDATE paper_bets 调用"
        sql, params = update_calls[0][0]
        assert "UPDATE paper_bets" in sql
        assert params[0] == 5.50  # chain_cost
        assert params[1] == 0.55  # chain_avg
        # Phase 42.1: matched_shares 也应同步更新
        assert abs(params[2] - 10.0) < 0.01  # chain_shares = 5.50 / 0.55 = 10.0
        assert params[3] == "bet_001"

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.get_open_paper_bets")
    @patch("main.connect")
    @patch("main.requests.get")
    @patch("main.get")
    def test_skips_manual_source(self, mock_cfg, mock_req, mock_conn, mock_open_bets):
        """manual 来源仓位应跳过对账。"""
        from main import _reconcile_with_chain

        mock_cfg.side_effect = lambda k: {
            "general.dry_run": False,
        }.get(k)

        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "conditionId": "0xcond_manual",
                "size": "5.0",
                "initialValue": "3.50",
                "avgPrice": "0.35",
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        mock_open_bets.return_value = [
            _make_open_bet(
                size_usdc=3.00,
                market_price=0.30,
                source="manual",
                condition_id="0xcond_manual",
            )
        ]

        ctx = mock_conn.return_value.__enter__.return_value
        _reconcile_with_chain()

        # manual 来源不应有 UPDATE
        ctx.execute.assert_not_called()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.get_open_paper_bets")
    @patch("main.connect")
    @patch("main.requests.get")
    @patch("main.get")
    def test_skips_within_threshold(
        self, mock_cfg, mock_req, mock_conn, mock_open_bets
    ):
        """差异在阈值内不触发更新。"""
        from main import _reconcile_with_chain

        mock_cfg.side_effect = lambda k: {
            "general.dry_run": False,
        }.get(k)

        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "conditionId": "0xcond_small",
                "size": "5.0",
                "initialValue": "3.505",
                "avgPrice": "0.5505",
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        mock_open_bets.return_value = [
            _make_open_bet(
                size_usdc=3.50,
                market_price=0.55,
                source="auto",
                condition_id="0xcond_small",
            )
        ]

        ctx = mock_conn.return_value.__enter__.return_value
        _reconcile_with_chain()

        # 差异 <0.01 USDC 和 <0.005 price → 不更新
        ctx.execute.assert_not_called()


# ---------------------------------------------------------------------------
# 5. PositionSyncer._reconcile_auto_positions
# ---------------------------------------------------------------------------


class TestSyncerReconcile:
    """验证 PositionSyncer 的 auto 仓位对账。"""

    @patch("src.sync.position_syncer.db")
    def test_reconciles_auto_positions(self, mock_db):
        """auto 仓位与链上数据不一致时应更新。"""
        mock_db.get_open_paper_bets.return_value = [
            _make_open_bet(
                bet_id="bet_auto_1",
                size_usdc=1.66,
                market_price=0.221,
                source="auto",
                condition_id="0xcond_hail",
            )
        ]

        chain_positions = [
            {
                "conditionId": "0xcond_hail",
                "size": "12.0",
                "initialValue": "2.76",
                "avgPrice": "0.22",
            }
        ]

        mock_connect_ctx = MagicMock()
        mock_db.connect.return_value.__enter__ = MagicMock(
            return_value=mock_connect_ctx
        )
        mock_db.connect.return_value.__exit__ = MagicMock(return_value=False)

        result = PositionSyncer._reconcile_auto_positions(chain_positions)

        assert result == 1
        # 可能含 scale-in INSERT + scale_in_count UPDATE + calibration UPDATE
        assert mock_connect_ctx.execute.call_count >= 1
        update_calls = [
            c
            for c in mock_connect_ctx.execute.call_args_list
            if len(c[0]) >= 1 and "UPDATE paper_bets SET size_usdc" in str(c[0][0])
        ]
        assert len(update_calls) >= 1
        sql, params = update_calls[0][0]
        assert "UPDATE paper_bets" in sql
        assert params[0] == 2.76  # chain initialValue
        assert params[1] == 0.22  # chain avgPrice
        assert params[2] == 12.0  # chain matched_shares (Phase 54: added to UPDATE)
        assert params[3] == "bet_auto_1"

    @patch("src.sync.position_syncer.db")
    def test_skips_manual_source(self, mock_db):
        """manual 来源不进行对账。"""
        mock_db.get_open_paper_bets.return_value = [
            _make_open_bet(
                size_usdc=1.80,
                market_price=0.36,
                source="manual",
                condition_id="0xcond_manual",
            )
        ]

        chain_positions = [
            {
                "conditionId": "0xcond_manual",
                "size": "5.0",
                "initialValue": "1.80",
                "avgPrice": "0.36",
            }
        ]

        result = PositionSyncer._reconcile_auto_positions(chain_positions)
        assert result == 0

    @patch("src.sync.position_syncer.db")
    def test_skips_no_condition_id(self, mock_db):
        """无 condition_id 的仓位不进行对账。"""
        mock_db.get_open_paper_bets.return_value = [
            _make_open_bet(
                size_usdc=2.0,
                market_price=0.50,
                source="auto",
                condition_id="",
            )
        ]

        chain_positions = [
            {
                "conditionId": "0xcond_other",
                "size": "5.0",
                "initialValue": "2.50",
                "avgPrice": "0.50",
            }
        ]

        result = PositionSyncer._reconcile_auto_positions(chain_positions)
        assert result == 0

    @patch("src.sync.position_syncer.db")
    def test_skips_when_chain_size_zero(self, mock_db):
        """链上 size=0 的仓位不参与对账。"""
        mock_db.get_open_paper_bets.return_value = [
            _make_open_bet(
                size_usdc=2.0,
                market_price=0.50,
                source="auto",
                condition_id="0xcond_zero",
            )
        ]

        chain_positions = [
            {
                "conditionId": "0xcond_zero",
                "size": "0",
                "initialValue": "0",
                "avgPrice": "0",
            }
        ]

        result = PositionSyncer._reconcile_auto_positions(chain_positions)
        assert result == 0


# ---------------------------------------------------------------------------
# 6. Probe + Main 合并后 filled_size 正确性
# ---------------------------------------------------------------------------


class TestProbeMainMerge:
    """验证 probe+main 合并后 filled_size 包含 probe 成本。"""

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_merged_filled_size_includes_probe(self, mock_get, _mock_dry):
        """probe_filled + main_filled = total filled_size。"""
        mock_get.side_effect = lambda k: {
            "general.dry_run": False,
            "executor.probe_enabled": True,
            "executor.probe_size_usdc": 1.10,
            "executor.clob_min_order_usdc": 1.10,
            "executor.slippage_threshold": 0.05,
            "executor.offset_pct": 0.001,
            "executor.min_bet_usdc": 1.0,
            "executor.min_order_size_shares": 2,
            "executor.fill_timeout_sec": 1,
            "executor.fill_poll_interval_sec": 0.1,
            "executor.vwap_enabled": False,
            "risk.max_single_market_pct": 0.10,
        }.get(k)

        executor = OrderExecutor()
        mock_client = MagicMock()
        mock_client.update_balance_allowance.return_value = None

        # 余额查询：probe 前20, probe 后18.90, main 前18.90, main 后17.10
        balance_iter = iter([20.0, 18.90, 18.90, 17.10])

        def mock_bal(params):
            bal = next(balance_iter, 17.10)
            return {"balance": str(int(bal * 1_000_000)), "allowances": {}}

        mock_client.get_balance_allowance.side_effect = mock_bal
        mock_client.create_and_post_order.return_value = {"orderID": "ord_probe"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "2.0",
            "price": "0.55",
        }

        executor._client = mock_client

        sig = make_signal(size_usdc=3.0)
        sig.expected_price = 0.55
        sig.token_id = "0xtok_yes"

        # Mock _check_balance to return live balance
        with patch.object(executor, "_check_balance", return_value=20.0):
            result = executor.execute(sig)

        assert result.success is True
        # filled_size 应包括 probe 和 main 的总成本
        assert result.filled_size > 0


# ---------------------------------------------------------------------------
# 7. Phase34: 对账时间守卫 — 新仓位跳过对账
# ---------------------------------------------------------------------------


class TestReconcileTimeGuard:
    """验证 _reconcile_with_chain 跳过刚下单的仓位。"""

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.get_open_paper_bets")
    @patch("main.connect")
    @patch("main.requests.get")
    @patch("main.get")
    def test_skips_new_bet_within_min_age(
        self, mock_cfg, mock_req, mock_conn, mock_open_bets
    ):
        """刚下单的仓位（< min_age_seconds）应跳过对账，防止读到不完整的链上数据。"""
        from main import _reconcile_with_chain

        mock_cfg.side_effect = lambda k: {
            "general.dry_run": False,
            "reconcile.min_age_seconds": 300,
        }.get(k)

        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "conditionId": "0xcond_new",
                "size": "2.0",
                "initialValue": "1.82",
                "avgPrice": "0.91",
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        # 仓位刚创建 10 秒前
        mock_open_bets.return_value = [
            _make_open_bet(
                bet_id="bet_new",
                size_usdc=3.68,
                market_price=0.92,
                source="auto",
                condition_id="0xcond_new",
                ts_entered=time.time() - 10,
            )
        ]

        ctx = mock_conn.return_value.__enter__.return_value
        _reconcile_with_chain()

        # 不应执行 UPDATE
        ctx.execute.assert_not_called()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.connect")
    @patch("main.requests.get")
    @patch("main.get")
    def test_reconciles_old_bet(
        self, mock_cfg, mock_req, mock_conn, mock_open_bets, _mock_dry
    ):
        """超过 min_age_seconds 的仓位正常对账。"""
        from main import _reconcile_with_chain

        mock_cfg.side_effect = lambda k: {
            "general.dry_run": False,
            "reconcile.min_age_seconds": 300,
        }.get(k)

        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "conditionId": "0xcond_old",
                "size": "4.0",
                "initialValue": "3.64",
                "avgPrice": "0.91",
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        # 仓位创建 10 分钟前
        mock_open_bets.return_value = [
            _make_open_bet(
                bet_id="bet_old",
                size_usdc=3.68,
                market_price=0.92,
                source="auto",
                condition_id="0xcond_old",
                ts_entered=time.time() - 600,
            )
        ]

        ctx = mock_conn.return_value.__enter__.return_value
        _reconcile_with_chain()

        # 应执行 UPDATE
        ctx.execute.assert_called_once()
        sql, params = ctx.execute.call_args[0]
        assert params[0] == 3.64
        assert params[1] == 0.91


class TestSyncerReconcileTimeGuard:
    """验证 PositionSyncer._reconcile_auto_positions 跳过新仓位。"""

    @patch("src.sync.position_syncer.get")
    @patch("src.sync.position_syncer.db")
    def test_skips_new_bet(self, mock_db, mock_cfg):
        """新仓位应跳过对账。"""
        mock_cfg.side_effect = lambda k: {
            "reconcile.min_age_seconds": 300,
        }.get(k)

        mock_db.get_open_paper_bets.return_value = [
            _make_open_bet(
                bet_id="bet_recent",
                size_usdc=3.68,
                market_price=0.92,
                source="auto",
                condition_id="0xcond_recent",
                ts_entered=time.time() - 10,
            )
        ]

        chain_positions = [
            {
                "conditionId": "0xcond_recent",
                "size": "2.0",
                "initialValue": "1.82",
                "avgPrice": "0.91",
            }
        ]

        result = PositionSyncer._reconcile_auto_positions(chain_positions)
        assert result == 0


# ---------------------------------------------------------------------------
# 8. Phase34: Probe 小额订单阈值
# ---------------------------------------------------------------------------


class TestProbeMinOrderThreshold:
    """验证小额订单跳过 probe 直接下单。"""

    @patch("src.executor.order_executor.is_dry_run", return_value=False)
    @patch("src.executor.order_executor.get")
    def test_small_order_skips_probe(self, mock_get, _mock_dry):
        """订单金额 < probe_min_order_usdc 时跳过 probe 直接下单。"""
        mock_get.side_effect = lambda k: {
            "general.dry_run": False,
            "executor.probe_enabled": True,
            "executor.probe_min_order_usdc": 10.0,
            "executor.probe_size_usdc": 1.10,
            "executor.clob_min_order_usdc": 1.10,
            "executor.slippage_threshold": 0.05,
            "executor.offset_pct": 0.001,
            "executor.min_bet_usdc": 1.0,
            "executor.min_order_size_shares": 2,
            "executor.fill_timeout_sec": 1,
            "executor.fill_poll_interval_sec": 0.1,
            "executor.vwap_enabled": False,
            "risk.max_single_market_pct": 0.10,
        }.get(k)

        executor = OrderExecutor()
        sig = make_signal(size_usdc=3.0)  # < 10.0 threshold
        sig.expected_price = 0.55
        sig.token_id = "0xtok_yes"

        with patch.object(executor, "_check_balance", return_value=20.0), patch.object(
            executor, "_probe_order"
        ) as mock_probe, patch.object(executor, "_submit_order") as mock_submit:
            mock_submit.return_value = OrderResult(
                signal=sig,
                success=True,
                tx_hash="0xabc",
                filled_size=3.0,
                actual_price=0.55,
            )
            result = executor.execute(sig)

        # probe 不应被调用
        mock_probe.assert_not_called()
        # 直接调用 _submit_order
        mock_submit.assert_called_once()
        assert result.success is True


# ---------------------------------------------------------------------------
# 9. Phase34b: _verify_trade_on_chain 成交新鲜度检查
# ---------------------------------------------------------------------------


class TestVerifyTradeOnChainFreshness:
    """验证 _verify_trade_on_chain 拒绝过期成交记录。"""

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.requests.get")
    def test_rejects_stale_trade(self, mock_req, mock_cfg):
        """超过 max_age 的成交记录应返回 verified=False。"""
        mock_cfg.side_effect = lambda k: {
            "general.dry_run": False,
            "reconcile.min_age_seconds": 300,
        }.get(k)

        executor = OrderExecutor()

        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "price": "0.22",
                "size": "7.56",
                "timestamp": str(int(time.time()) - 600),  # 10 min ago
                "transactionHash": "0xdeadbeef",
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        result = executor._verify_trade_on_chain("0xcond_test", side="BUY")
        assert result["verified"] is False

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.requests.get")
    def test_accepts_fresh_trade(self, mock_req, mock_cfg):
        """新鲜的成交记录应返回 verified=True。"""
        mock_cfg.side_effect = lambda k: {
            "general.dry_run": False,
            "reconcile.min_age_seconds": 300,
        }.get(k)

        executor = OrderExecutor()

        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "price": "0.31",
                "size": "8.59",
                "timestamp": str(int(time.time()) - 5),  # 5 sec ago
                "transactionHash": "0xfreshbeef",
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        result = executor._verify_trade_on_chain("0xcond_test", side="BUY")
        assert result["verified"] is True
        assert result["price"] == 0.31
        assert result["size"] == 8.59


# ---------------------------------------------------------------------------
# 10. Phase34b: Data API 校准安全防护 — 拒绝大幅缩小
# ---------------------------------------------------------------------------


class TestDataApiCalibrationSafety:
    """验证 Data API 校准不会用陈旧数据覆盖正确的余额校准值。"""

    @patch("src.executor.order_executor.get")
    def test_rejects_cost_halving_calibration(self, mock_get):
        """当 chain_cost < actual_cost×0.5 时，拒绝校准。"""
        mock_get.side_effect = lambda k: {
            "general.dry_run": False,
            "executor.offset_pct": 0.001,
            "executor.clob_min_order_usdc": 1.10,
            "executor.min_bet_usdc": 1.0,
            "executor.min_order_size_shares": 2,
            "executor.fill_timeout_sec": 1,
            "executor.fill_poll_interval_sec": 0.1,
            "risk.max_single_market_pct": 0.05,
            "reconcile.min_age_seconds": 300,
        }.get(k)

        executor = OrderExecutor()
        mock_client = MagicMock()
        mock_client.create_and_post_order.return_value = {"orderID": "order_999"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "8.59",
            "price": "0.311",
        }

        # balance_before=20, balance_after=17.34 → delta=2.66 (correct cost)
        balance_sequence = iter([20.0, 17.34])

        def mock_balance_allowance(params):
            bal = next(balance_sequence, 17.34)
            return {"balance": str(int(bal * 1_000_000)), "allowances": {}}

        mock_client.get_balance_allowance.side_effect = mock_balance_allowance
        mock_client.update_balance_allowance.return_value = None
        executor._client = mock_client

        # Simulate _verify_trade_on_chain returning STALE data
        with patch.object(
            executor,
            "_verify_trade_on_chain",
            return_value={
                "verified": True,
                "price": 0.22,  # stale price
                "size": 7.56,  # stale size → cost = 1.66
            },
        ):
            sig = make_signal(size_usdc=2.67)
            sig.expected_price = 0.31
            sig.token_id = "0xtok_yes"
            sig.condition_id = "0xcond_hail"

            result = executor._submit_order(sig)

        assert result.success is True
        # 关键验证：actual_price 不应被覆盖为陈旧的 0.22
        # 应保留余额校准值或限价
        assert (
            result.actual_price > 0.25
        ), f"Price should NOT be stale 0.22, got {result.actual_price}"


# ---------------------------------------------------------------------------
# 10. Phase35: 幽灵仓位检测 — 跳过所有主动关闭的exit_type
# ---------------------------------------------------------------------------


def _make_closed_bet(
    bet_id: str = None,
    condition_id: str = "0xcond_ghost",
    question: str = "Will it rain?",
    exit_type: str = "write_off",
    resolved_outcome: str = None,
):
    """构造一个已关闭的 paper_bet 字典，用于 get_recently_closed_bets mock。"""
    return {
        "bet_id": bet_id or str(uuid.uuid4()),
        "market_id": "mkt_ghost",
        "question": question,
        "outcome": "YES",
        "condition_id": condition_id,
        "exit_type": exit_type,
        "resolved_outcome": resolved_outcome,
    }


class TestReconcileGhostDetection:
    """Phase35: 验证幽灵仓位检测跳过所有主动关闭的 exit_type。

    BUG 教训：仅跳过 write_off 导致 profit_take 仓位被重开，
    在更高价格止损形成「买高卖低」亏损。
    """

    def _run_ghost_detection(
        self,
        mock_cfg,
        mock_req,
        mock_open_bets,
        mock_closed,
        mock_reopen,
        chain_positions,
        closed_bets,
    ):
        """辅助方法：配置 mock 并执行 _reconcile_with_chain。"""
        from main import _reconcile_with_chain

        mock_cfg.side_effect = lambda k: {
            "general.dry_run": False,
            "reconcile.min_age_seconds": 300,
            "reconcile.min_ghost_shares": 1.0,
        }.get(k)

        mock_resp = MagicMock()
        mock_resp.json.return_value = chain_positions
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp
        mock_open_bets.return_value = []
        mock_closed.return_value = closed_bets
        _reconcile_with_chain()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.reopen_ghost_position")
    @patch("main.get_recently_closed_bets")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.requests.get")
    @patch("main.get")
    def test_skip_profit_take_ghost(
        self, mock_cfg, mock_req, mock_open_bets, _dry, mock_closed, mock_reopen
    ):
        """profit_take 已关闭仓位不应被重开（核心 BUG 修复验证）。"""
        chain = [{"conditionId": "0xcond_pt", "size": "5.0"}]
        closed = [
            _make_closed_bet(
                bet_id="bet_pt", condition_id="0xcond_pt", exit_type="profit_take"
            )
        ]
        self._run_ghost_detection(
            mock_cfg,
            mock_req,
            mock_open_bets,
            mock_closed,
            mock_reopen,
            chain,
            closed,
        )
        mock_reopen.assert_not_called()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.reopen_ghost_position")
    @patch("main.get_recently_closed_bets")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.requests.get")
    @patch("main.get")
    def test_skip_stop_loss_ghost(
        self, mock_cfg, mock_req, mock_open_bets, _dry, mock_closed, mock_reopen
    ):
        """stop_loss 已关闭仓位不应被重开。"""
        chain = [{"conditionId": "0xcond_sl", "size": "3.0"}]
        closed = [
            _make_closed_bet(
                bet_id="bet_sl", condition_id="0xcond_sl", exit_type="stop_loss"
            )
        ]
        self._run_ghost_detection(
            mock_cfg,
            mock_req,
            mock_open_bets,
            mock_closed,
            mock_reopen,
            chain,
            closed,
        )
        mock_reopen.assert_not_called()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.reopen_ghost_position")
    @patch("main.get_recently_closed_bets")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.requests.get")
    @patch("main.get")
    def test_skip_write_off_ghost(
        self, mock_cfg, mock_req, mock_open_bets, _dry, mock_closed, mock_reopen
    ):
        """write_off 已关闭仓位不应被重开（既有行为保持）。"""
        chain = [{"conditionId": "0xcond_wo", "size": "2.0"}]
        closed = [
            _make_closed_bet(
                bet_id="bet_wo", condition_id="0xcond_wo", exit_type="write_off"
            )
        ]
        self._run_ghost_detection(
            mock_cfg,
            mock_req,
            mock_open_bets,
            mock_closed,
            mock_reopen,
            chain,
            closed,
        )
        mock_reopen.assert_not_called()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.reopen_ghost_position")
    @patch("main.get_recently_closed_bets")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.requests.get")
    @patch("main.get")
    def test_skip_manual_exit_ghost(
        self, mock_cfg, mock_req, mock_open_bets, _dry, mock_closed, mock_reopen
    ):
        """manual_exit 已关闭仓位不应被重开。"""
        chain = [{"conditionId": "0xcond_me", "size": "4.0"}]
        closed = [
            _make_closed_bet(
                bet_id="bet_me", condition_id="0xcond_me", exit_type="manual_exit"
            )
        ]
        self._run_ghost_detection(
            mock_cfg,
            mock_req,
            mock_open_bets,
            mock_closed,
            mock_reopen,
            chain,
            closed,
        )
        mock_reopen.assert_not_called()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.reopen_ghost_position", return_value=True)
    @patch("main.get_recently_closed_bets")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.requests.get")
    @patch("main.get")
    def test_reopens_resolution_ghost(
        self, mock_cfg, mock_req, mock_open_bets, _dry, mock_closed, mock_reopen
    ):
        """resolution exit_type 的仓位若仍有链上 shares 则应触发重开（崩溃恢复场景）。"""
        chain = [{"conditionId": "0xcond_res", "size": "5.0"}]
        closed = [
            _make_closed_bet(
                bet_id="bet_res", condition_id="0xcond_res", exit_type="resolution"
            )
        ]
        self._run_ghost_detection(
            mock_cfg,
            mock_req,
            mock_open_bets,
            mock_closed,
            mock_reopen,
            chain,
            closed,
        )
        mock_reopen.assert_called_once_with("bet_res")


# ---------------------------------------------------------------------------
# Phase 36: 已结算仓位(resolved_outcome) 跳过 Ghost 重开
# ---------------------------------------------------------------------------


class TestGhostSkipResolved:
    """Phase 36: 有 resolved_outcome 的仓位不应被 Ghost 检测重开。"""

    def _run_ghost_detection(
        self,
        mock_cfg,
        mock_req,
        mock_open_bets,
        mock_closed,
        mock_reopen,
        chain_positions,
        closed_bets,
    ):
        from main import _reconcile_with_chain

        mock_cfg.side_effect = lambda k: {
            "general.dry_run": False,
            "reconcile.min_age_seconds": 300,
            "reconcile.min_ghost_shares": 1.0,
        }.get(k)
        mock_resp = MagicMock()
        mock_resp.json.return_value = chain_positions
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp
        mock_open_bets.return_value = []
        mock_closed.return_value = closed_bets
        _reconcile_with_chain()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.reopen_ghost_position")
    @patch("main.get_recently_closed_bets")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.requests.get")
    @patch("main.get")
    def test_skip_ghost_with_resolved_outcome(
        self, mock_cfg, mock_req, mock_open_bets, _dry, mock_closed, mock_reopen
    ):
        """resolved_outcome 已有值的仓位不应被重开，即使 exit_type=resolution。"""
        chain = [{"conditionId": "0xcond_resolved", "size": "10.0"}]
        closed = [
            _make_closed_bet(
                bet_id="bet_resolved",
                condition_id="0xcond_resolved",
                exit_type="resolution",
                resolved_outcome="Yes",
            )
        ]
        self._run_ghost_detection(
            mock_cfg,
            mock_req,
            mock_open_bets,
            mock_closed,
            mock_reopen,
            chain,
            closed,
        )
        mock_reopen.assert_not_called()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.reopen_ghost_position", return_value=True)
    @patch("main.get_recently_closed_bets")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.requests.get")
    @patch("main.get")
    def test_reopen_ghost_without_resolved_outcome(
        self, mock_cfg, mock_req, mock_open_bets, _dry, mock_closed, mock_reopen
    ):
        """exit_type=resolution 但 resolved_outcome=None → 应触发重开（崩溃恢复）。"""
        chain = [{"conditionId": "0xcond_crash", "size": "8.0"}]
        closed = [
            _make_closed_bet(
                bet_id="bet_crash",
                condition_id="0xcond_crash",
                exit_type="resolution",
                resolved_outcome=None,
            )
        ]
        self._run_ghost_detection(
            mock_cfg,
            mock_req,
            mock_open_bets,
            mock_closed,
            mock_reopen,
            chain,
            closed,
        )
        mock_reopen.assert_called_once_with("bet_crash")


# ---------------------------------------------------------------------------
# Phase 36: Phantom 仓位自动核销测试
# ---------------------------------------------------------------------------


class TestPhantomAutoWriteOff:
    """Phase 36: DB显示OPEN但链上无持仓 → 自动 write_off。"""

    def _run_reconcile(
        self,
        mock_cfg,
        mock_req,
        mock_open_bets,
        mock_closed,
        chain_positions,
        open_bets,
        mock_write_off=None,
    ):
        from main import _reconcile_with_chain

        mock_cfg.side_effect = lambda k: {
            "general.dry_run": False,
            "reconcile.min_age_seconds": 300,
            "reconcile.min_ghost_shares": 1.0,
        }.get(k)
        mock_resp = MagicMock()
        mock_resp.json.return_value = chain_positions
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp
        mock_open_bets.return_value = open_bets
        mock_closed.return_value = []
        _reconcile_with_chain()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.close_paper_bet_stop_loss")
    @patch("main.get_recently_closed_bets")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.requests.get")
    @patch("main.get")
    def test_phantom_auto_write_off(
        self, mock_cfg, mock_req, mock_open_bets, _dry, mock_closed, mock_write_off
    ):
        """source=auto + 老仓(>300s) + 链上无持仓 → 自动核销。"""
        open_bets = [
            _make_open_bet(
                bet_id="bet_phantom",
                condition_id="0xcond_phantom",
                source="auto",
                ts_entered=time.time() - 600,
            )
        ]
        # 链上空（无任何持仓）
        self._run_reconcile(
            mock_cfg, mock_req, mock_open_bets, mock_closed, [], open_bets
        )
        # Phase 37: write_off 现在必须传递完整参数
        mock_write_off.assert_called_once_with(
            bet_id="bet_phantom",
            exit_price=0.0,
            pnl=-3.0,  # -size_usdc
            payout=0.0,
        )

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.close_paper_bet_write_off")
    @patch("main.get_recently_closed_bets")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.requests.get")
    @patch("main.get")
    def test_phantom_skip_new_bet(
        self, mock_cfg, mock_req, mock_open_bets, _dry, mock_closed, mock_write_off
    ):
        """刚下单的仓(age < 300s) 不应被 Phantom 核销。"""
        open_bets = [
            _make_open_bet(
                bet_id="bet_new",
                condition_id="0xcond_new",
                source="auto",
                ts_entered=time.time() - 60,  # 仅60秒，太新
            )
        ]
        self._run_reconcile(
            mock_cfg, mock_req, mock_open_bets, mock_closed, [], open_bets
        )
        mock_write_off.assert_not_called()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.close_paper_bet_write_off")
    @patch("main.get_recently_closed_bets")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.requests.get")
    @patch("main.get")
    def test_phantom_skip_manual_source(
        self, mock_cfg, mock_req, mock_open_bets, _dry, mock_closed, mock_write_off
    ):
        """source=manual 仓位不参与 Phantom 检测（可能不在链上）。"""
        open_bets = [
            _make_open_bet(
                bet_id="bet_manual",
                condition_id="0xcond_manual",
                source="manual",
                ts_entered=time.time() - 600,
            )
        ]
        self._run_reconcile(
            mock_cfg, mock_req, mock_open_bets, mock_closed, [], open_bets
        )
        mock_write_off.assert_not_called()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.close_paper_bet_write_off")
    @patch("main.get_recently_closed_bets")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.requests.get")
    @patch("main.get")
    def test_phantom_skip_if_on_chain(
        self, mock_cfg, mock_req, mock_open_bets, _dry, mock_closed, mock_write_off
    ):
        """链上有对应持仓的仓位不应被 Phantom 核销。"""
        open_bets = [
            _make_open_bet(
                bet_id="bet_normal",
                condition_id="0xcond_normal",
                source="auto",
                ts_entered=time.time() - 600,
            )
        ]
        chain = [{"conditionId": "0xcond_normal", "size": "5.0"}]
        self._run_reconcile(
            mock_cfg, mock_req, mock_open_bets, mock_closed, chain, open_bets
        )
        mock_write_off.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 36: had_recent_exit_by_condition DB函数测试
# ---------------------------------------------------------------------------


class TestHadRecentExitByCondition:
    """Phase 36: 验证 condition_id 级别的退出冷却查询。"""

    @pytest.fixture(autouse=True)
    def _setup_db(self, tmp_path):
        self.db_path = tmp_path / "test.db"
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            """CREATE TABLE paper_bets (
                bet_id TEXT PRIMARY KEY,
                condition_id TEXT,
                exit_type TEXT,
                resolved_at REAL
            )"""
        )
        conn.commit()
        conn.close()

    def _insert_bet(self, condition_id, exit_type, resolved_at):
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "INSERT INTO paper_bets (bet_id, condition_id, exit_type, resolved_at) "
            "VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), condition_id, exit_type, resolved_at),
        )
        conn.commit()
        conn.close()

    def test_recent_profit_take_blocks(self):
        """冷却期内有 profit_take → 返回 True。"""
        from src.utils.db import had_recent_exit_by_condition

        self._insert_bet("0xcond_1", "profit_take", time.time() - 1800)  # 30分钟前
        assert had_recent_exit_by_condition("0xcond_1", 1.0, self.db_path) is True

    def test_recent_stop_loss_blocks(self):
        """冷却期内有 stop_loss → 返回 True。"""
        from src.utils.db import had_recent_exit_by_condition

        self._insert_bet("0xcond_2", "stop_loss", time.time() - 600)  # 10分钟前
        assert had_recent_exit_by_condition("0xcond_2", 1.0, self.db_path) is True

    def test_old_exit_does_not_block(self):
        """冷却期已过的退出 → 返回 False。"""
        from src.utils.db import had_recent_exit_by_condition

        self._insert_bet("0xcond_3", "profit_take", time.time() - 7200)  # 2小时前
        assert had_recent_exit_by_condition("0xcond_3", 1.0, self.db_path) is False

    def test_no_exit_does_not_block(self):
        """无退出记录 → 返回 False。"""
        from src.utils.db import had_recent_exit_by_condition

        assert had_recent_exit_by_condition("0xcond_none", 1.0, self.db_path) is False

    def test_resolution_exit_does_not_block(self):
        """exit_type=resolution 不在冷却列表中 → 返回 False。"""
        from src.utils.db import had_recent_exit_by_condition

        self._insert_bet("0xcond_4", "resolution", time.time() - 600)
        assert had_recent_exit_by_condition("0xcond_4", 1.0, self.db_path) is False


# ---------------------------------------------------------------------------
# Phase 37: exit_type DEFAULT 迁移 + update_paper_bet_outcome 测试
# ---------------------------------------------------------------------------


class TestExitTypeSchema:
    """Phase 37: 验证 exit_type DEFAULT 修复和 migration v8。"""

    @pytest.fixture(autouse=True)
    def _setup_db(self, tmp_path):
        self.db_path = tmp_path / "test.db"
        from src.utils.db import init_db

        init_db(self.db_path)

    def test_new_bet_has_null_exit_type(self):
        """新插入的 bet 的 exit_type 应为 NULL，不再是 'resolution'。"""
        from src.utils.db import connect, insert_paper_bet

        bet = {
            "bet_id": "test_null_exit",
            "ts_entered": time.time(),
            "market_id": "mkt_test",
            "question": "Test?",
            "category": "test",
            "outcome": "YES",
            "confidence": 0.7,
            "ai_probability": 0.65,
            "market_price": 0.55,
            "ev": 0.10,
            "size_usdc": 2.0,
            "sim_balance_before": 100.0,
            "sim_balance_after": 98.0,
            "market_end_date": "2026-12-31",
            "weather_context": "",
            "ai_models": "test",
            "cycle_id": 1,
        }
        insert_paper_bet(bet, self.db_path)
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT exit_type FROM paper_bets WHERE bet_id='test_null_exit'"
            ).fetchone()
        assert row["exit_type"] is None

    def test_update_paper_bet_outcome_sets_exit_type_resolution(self):
        """update_paper_bet_outcome 应显式设置 exit_type='resolution'。"""
        from src.utils.db import connect, insert_paper_bet, update_paper_bet_outcome

        bet = {
            "bet_id": "test_resolve",
            "ts_entered": time.time(),
            "market_id": "mkt_test",
            "question": "Test resolve?",
            "category": "test",
            "outcome": "YES",
            "confidence": 0.7,
            "ai_probability": 0.65,
            "market_price": 0.55,
            "ev": 0.10,
            "size_usdc": 2.0,
            "sim_balance_before": 100.0,
            "sim_balance_after": 98.0,
            "market_end_date": "2026-12-31",
            "weather_context": "",
            "ai_models": "test",
            "cycle_id": 1,
        }
        insert_paper_bet(bet, self.db_path)
        # 验证插入后 exit_type 为 NULL
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT exit_type FROM paper_bets WHERE bet_id='test_resolve'"
            ).fetchone()
        assert row["exit_type"] is None

        # 解析市场
        update_paper_bet_outcome(
            "test_resolve", "YES", time.time(), 2.0, 3.64, 1, self.db_path
        )
        with connect(self.db_path) as conn:
            row = conn.execute(
                "SELECT exit_type FROM paper_bets WHERE bet_id='test_resolve'"
            ).fetchone()
        assert row["exit_type"] == "resolution"


class TestForceWriteOffChainVerification:
    """Phase 37: _force_write_off_position 在核销前验证链上状态。"""

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.increment_sell_fail_count")
    @patch("main.close_paper_bet_write_off")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.requests.get")
    def test_write_off_blocked_when_chain_has_shares(
        self, mock_req, _dry, mock_write_off, mock_inc_fail
    ):
        """链上仍有 >0.5 shares 时，不应核销，应递增 sell_fail_count。"""
        from main import _force_write_off_position

        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"conditionId": "0xcond_abc", "size": "3.5"}]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        bet = {
            "bet_id": "bet_chain_exists",
            "market_price": 0.55,
            "exit_price": 0.40,
            "size_usdc": 2.0,
            "condition_id": "0xcond_abc",
        }

        _force_write_off_position(bet, trigger="below_min_order_size")
        mock_write_off.assert_not_called()
        mock_inc_fail.assert_called_once_with("bet_chain_exists")

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.close_paper_bet_stop_loss")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.requests.get")
    def test_write_off_proceeds_when_chain_empty(self, mock_req, _dry, mock_close):
        """链上无持仓时，应正常核销。"""
        from main import _force_write_off_position

        mock_resp = MagicMock()
        mock_resp.json.return_value = []  # 链上无持仓
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        bet = {
            "bet_id": "bet_chain_empty",
            "market_price": 0.55,
            "exit_price": 0.0,
            "size_usdc": 2.0,
            "condition_id": "0xcond_dead",
        }

        _force_write_off_position(bet, trigger="timeout_max_retries")
        # T55: timeout trigger (no "profit_take"/"certainty") → close_paper_bet_stop_loss
        mock_close.assert_called_once()

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.close_paper_bet_write_off")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.requests.get")
    def test_write_off_skipped_on_api_failure(self, mock_req, _dry, mock_write_off):
        """Data API 查询失败时，保守不核销（fail-safe）。"""
        import requests as real_requests
        from main import _force_write_off_position

        mock_req.side_effect = real_requests.RequestException("timeout")

        bet = {
            "bet_id": "bet_api_fail",
            "market_price": 0.55,
            "exit_price": 0.0,
            "size_usdc": 2.0,
            "condition_id": "0xcond_api_fail",
        }

        _force_write_off_position(bet, trigger="timeout_max_retries")
        mock_write_off.assert_not_called()

    @patch("main.close_paper_bet_stop_loss")
    @patch("main.is_dry_run", return_value=True)
    def test_write_off_in_dry_run_skips_chain_check(self, _dry, mock_close):
        """DRY RUN 模式下跳过链上验证，直接核销。"""
        from main import _force_write_off_position

        bet = {
            "bet_id": "bet_dry",
            "market_price": 0.55,
            "exit_price": 0.0,
            "size_usdc": 2.0,
            "condition_id": "0xcond_dry",
        }

        _force_write_off_position(bet, trigger="below_min_order_size")
        # T55: below_min trigger (no "profit_take") → close_paper_bet_stop_loss
        mock_close.assert_called_once()
        mock_close.assert_called_once()


# ---------------------------------------------------------------------------
# SUPREME 1: below_min_order_size + 市场未resolved → 阻止核销
# ---------------------------------------------------------------------------


class TestForceWriteOffSupreme1:
    """SUPREME 1 修复: below_min_order_size 核销前必须检查市场resolved状态。

    场景:
    - shares < min_order_size → CLOB无法卖出
    - 但Token仍在链上，市场未resolved → 禁止核销，保持OPEN
    - 市场已resolved → 允许核销（Token已无价值或已赎回）
    """

    @patch("main.increment_sell_fail_count")
    @patch("main.close_paper_bet_profit_take")
    @patch("main.close_paper_bet_stop_loss")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.requests.get")
    def test_blocks_writeoff_when_market_not_resolved(
        self, mock_req, _dry, mock_sl, mock_pt, mock_inc
    ):
        """实盘 + below_min_order_size + 市场未resolved → 阻止核销。"""
        from main import _force_write_off_position

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"resolved": False, "question": "Test?"}
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        bet = {
            "bet_id": "31961560_test",
            "market_price": 0.20,
            "exit_price": 0.998,
            "size_usdc": 1.0,
            "condition_id": "0xcond_test",
            "market_id": "mkt_istanbul_test",
        }

        _force_write_off_position(
            bet,
            trigger="profit_take_min_shares",
            reason="below_min_order_size_force_writeoff",
        )

        # Must NOT close DB
        mock_pt.assert_not_called()
        mock_sl.assert_not_called()
        # Must increment sell_fail_count
        mock_inc.assert_called_once_with("31961560_test")

    @patch("main.increment_sell_fail_count")
    @patch("main.close_paper_bet_profit_take")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.requests.get")
    def test_allows_writeoff_when_market_resolved(
        self, mock_req, _dry, mock_pt, mock_inc
    ):
        """实盘 + below_min_order_size + 市场已resolved → 允许核销。"""
        from main import _force_write_off_position

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"resolved": True, "question": "Test?"}
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        bet = {
            "bet_id": "resolved_test_01",
            "market_price": 0.20,
            "exit_price": 0.998,
            "size_usdc": 1.0,
            "condition_id": "0xcond_resolved",
            "market_id": "mkt_resolved_test",
        }

        _force_write_off_position(
            bet,
            trigger="profit_take_min_shares",
            reason="below_min_order_size_force_writeoff",
        )

        # Market resolved → DB close allowed
        mock_pt.assert_called_once()
        mock_inc.assert_not_called()

    @patch("main.increment_sell_fail_count")
    @patch("main.close_paper_bet_stop_loss")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.requests.get")
    def test_blocks_writeoff_on_gamma_api_failure(
        self, mock_req, _dry, mock_sl, mock_inc
    ):
        """实盘 + Gamma API 失败 → 保守不核销（fail-safe）。"""
        import requests as real_requests
        from main import _force_write_off_position

        mock_req.side_effect = real_requests.RequestException("gamma timeout")

        bet = {
            "bet_id": "api_fail_test",
            "market_price": 0.30,
            "exit_price": 0.001,
            "size_usdc": 1.5,
            "condition_id": "0xcond_apifail",
            "market_id": "mkt_apifail_test",
        }

        _force_write_off_position(
            bet,
            trigger="stop_loss_min_shares",
            reason="below_min_order_size_force_writeoff",
        )

        # API failed → conservative → no writeoff
        mock_sl.assert_not_called()
        mock_inc.assert_called_once_with("api_fail_test")

    @patch("main.close_paper_bet_stop_loss")
    @patch("main.is_dry_run", return_value=True)
    def test_dry_run_allows_writeoff_regardless(self, _dry, mock_sl):
        """DRY RUN 模式下 below_min_order_size 直接核销，不查 resolved。"""
        from main import _force_write_off_position

        bet = {
            "bet_id": "dry_run_test",
            "market_price": 0.50,
            "exit_price": 0.0,
            "size_usdc": 2.0,
            "condition_id": "0xcond_dry",
            "market_id": "mkt_dry_test",
        }

        _force_write_off_position(
            bet,
            trigger="stop_loss_min_shares",
            reason="below_min_order_size_force_writeoff",
        )

        # DRY RUN → close allowed without checking resolved
        mock_sl.assert_called_once()

    @patch("main.increment_sell_fail_count")
    @patch("main.close_paper_bet_stop_loss")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.requests.get")
    def test_blocks_writeoff_100_times_still_open(
        self, mock_req, _dry, mock_sl, mock_inc
    ):
        """SUPREME 1 验证: 卖出失败100次后仍保持OPEN，不触发核销。"""
        from main import _force_write_off_position

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"resolved": False}
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        bet = {
            "bet_id": "retry_100_test",
            "market_price": 0.30,
            "exit_price": 0.001,
            "size_usdc": 1.5,
            "condition_id": "0xcond_retry",
            "market_id": "mkt_retry_test",
        }

        for _ in range(100):
            _force_write_off_position(
                bet,
                trigger="stop_loss_min_shares",
                reason="below_min_order_size_force_writeoff",
            )

        # After 100 attempts → still never closed DB
        mock_sl.assert_not_called()
        assert mock_inc.call_count == 100

    @patch("main.increment_sell_fail_count")
    @patch("main.close_paper_bet_stop_loss")
    @patch("main.is_dry_run", return_value=False)
    @patch("main.requests.get")
    def test_blocks_writeoff_no_market_id(self, mock_req, _dry, mock_sl, mock_inc):
        """market_id 为空 → 无法查询resolved → 保守不核销。"""
        from main import _force_write_off_position

        bet = {
            "bet_id": "no_mktid_test",
            "market_price": 0.30,
            "exit_price": 0.001,
            "size_usdc": 1.5,
            "condition_id": "0xcond_nomkt",
            "market_id": "",  # empty
        }

        _force_write_off_position(
            bet,
            trigger="stop_loss_min_shares",
            reason="below_min_order_size_force_writeoff",
        )

        # No market_id → can't check resolved → block writeoff
        mock_sl.assert_not_called()
        mock_inc.assert_called_once_with("no_mktid_test")
        # Should not call Gamma API
        mock_req.assert_not_called()


# ---------------------------------------------------------------------------
# Phase 42.1: 对账后 matched_shares 同步 + 小仓位警告
# ---------------------------------------------------------------------------


class TestReconcileMatchedSharesSync:
    """验证 _reconcile_with_chain 同步更新 matched_shares 的逻辑。"""

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.connect")
    @patch("main.requests.get")
    @patch("main.get")
    def test_matched_shares_updated_in_sql(
        self, mock_cfg, mock_req, mock_conn, mock_open_bets, _mock_dry
    ):
        """对账更新时 SQL 应包含 matched_shares 字段。"""
        from main import _reconcile_with_chain

        mock_cfg.side_effect = lambda k: {
            "general.dry_run": False,
            "reconcile.min_age_seconds": 0,
            "executor.min_order_size_shares": 5,
        }.get(k)

        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "conditionId": "0xcond_sync",
                "size": "8.0",
                "initialValue": "4.00",
                "avgPrice": "0.50",
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        mock_open_bets.return_value = [
            _make_open_bet(
                bet_id="bet_sync",
                size_usdc=3.50,
                market_price=0.45,
                source="auto",
                condition_id="0xcond_sync",
            )
        ]

        ctx = mock_conn.return_value.__enter__.return_value
        _reconcile_with_chain()

        ctx.execute.assert_called_once()
        sql, params = ctx.execute.call_args[0]
        assert "matched_shares" in sql
        # chain_shares = 4.00 / 0.50 = 8.0
        assert abs(params[2] - 8.0) < 0.01

    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xWallet123"})
    @patch("main.is_dry_run", return_value=False)
    @patch("main.get_open_paper_bets")
    @patch("main.connect")
    @patch("main.requests.get")
    @patch("main.get")
    def test_small_shares_after_reconcile_logs_warning(
        self, mock_cfg, mock_req, mock_conn, mock_open_bets, _mock_dry
    ):
        """对账后 shares < est_min_shares 时应记录警告日志。"""
        from main import _reconcile_with_chain

        mock_cfg.side_effect = lambda k: {
            "general.dry_run": False,
            "reconcile.min_age_seconds": 0,
            "executor.min_bet_usdc": 1.0,
        }.get(k)

        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "conditionId": "0xcond_small",
                "size": "1.0",
                "initialValue": "0.55",
                "avgPrice": "0.55",
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_req.return_value = mock_resp

        mock_open_bets.return_value = [
            _make_open_bet(
                bet_id="bet_small_reconcile",
                size_usdc=0.60,
                market_price=0.60,
                source="auto",
                condition_id="0xcond_small",
            )
        ]

        ctx = mock_conn.return_value.__enter__.return_value

        import logging

        with patch("main.log") as mock_log:
            _reconcile_with_chain()
            # chain_cost=0.55, chain_avg=0.55 → chain_shares=1.0
            # _est_min_shares=1.0/0.55=1.818 → 1.0 < 1.818 → warning
            warning_calls = [
                c for c in mock_log.warning.call_args_list if "对账后" in str(c)
            ]
            assert len(warning_calls) >= 1
            assert len(warning_calls) >= 1
            assert len(warning_calls) >= 1
            assert len(warning_calls) >= 1
            assert len(warning_calls) >= 1
            assert len(warning_calls) >= 1
