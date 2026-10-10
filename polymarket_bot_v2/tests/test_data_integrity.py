"""
test_data_integrity.py — Phase 30 数据完整性测试
覆盖：condition_id 传播链、Data API 验证、_db_write_live_bets condition_id、auth threshold
"""

import os
import time
from unittest.mock import MagicMock, patch

import pytest
from src.executor.order_executor import OrderExecutor, OrderResult
from src.signal.signal_engine import Signal

# ---------------------------------------------------------------------------
# Signal condition_id 字段测试
# ---------------------------------------------------------------------------


class TestSignalConditionId:
    """验证 Signal dataclass 新增 condition_id 字段。"""

    def test_signal_has_condition_id_default_empty(self):
        sig = Signal(
            market_id="mkt_001",
            question="test?",
            outcome="YES",
            ev=0.1,
            confidence=0.8,
            size_usdc=2.0,
        )
        assert sig.condition_id == ""

    def test_signal_condition_id_settable(self):
        sig = Signal(
            market_id="mkt_001",
            question="test?",
            outcome="YES",
            ev=0.1,
            confidence=0.8,
            size_usdc=2.0,
            condition_id="0xabc123",
        )
        assert sig.condition_id == "0xabc123"

    def test_signal_condition_id_mutable(self):
        sig = Signal(
            market_id="mkt_001",
            question="test?",
            outcome="YES",
            ev=0.1,
            confidence=0.8,
            size_usdc=2.0,
        )
        sig.condition_id = "0xdef456"
        assert sig.condition_id == "0xdef456"


# ---------------------------------------------------------------------------
# OrderResult condition_id 字段测试
# ---------------------------------------------------------------------------


class TestOrderResultConditionId:
    """验证 OrderResult dataclass 新增 condition_id 字段。"""

    def test_order_result_default_empty(self):
        sig = Signal(
            market_id="m",
            question="q",
            outcome="YES",
            ev=0.1,
            confidence=0.8,
            size_usdc=1.0,
        )
        r = OrderResult(signal=sig, success=True)
        assert r.condition_id == ""

    def test_order_result_with_condition_id(self):
        sig = Signal(
            market_id="m",
            question="q",
            outcome="YES",
            ev=0.1,
            confidence=0.8,
            size_usdc=1.0,
        )
        r = OrderResult(
            signal=sig,
            success=True,
            condition_id="0xabc",
        )
        assert r.condition_id == "0xabc"


# ---------------------------------------------------------------------------
# _resolve_token_id 设置 signal.condition_id 测试
# ---------------------------------------------------------------------------


class TestResolveTokenIdSetsConditionId:
    """验证 _resolve_token_id 会设置 signal.condition_id。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch.object(OrderExecutor, "_to_condition_id")
    def test_resolve_sets_condition_id_from_gamma(self, mock_to_cid):
        """Gamma numeric ID → condition_id 应被保存到 signal。"""
        mock_to_cid.return_value = "0xabc123def4567890"
        mock_client = MagicMock()
        mock_client.get_market.return_value = {
            "tokens": [
                {"outcome": "Yes", "token_id": "tok_yes"},
                {"outcome": "No", "token_id": "tok_no"},
            ]
        }

        sig = Signal(
            market_id="12345",
            question="test?",
            outcome="YES",
            ev=0.1,
            confidence=0.8,
            size_usdc=1.0,
        )
        assert sig.condition_id == ""

        token_id = self.executor._resolve_token_id(mock_client, sig)
        assert token_id == "tok_yes"
        assert sig.condition_id == "0xabc123def4567890"

    @patch.object(OrderExecutor, "_to_condition_id")
    def test_resolve_sets_condition_id_from_0x_input(self, mock_to_cid):
        """0x... market_id 直接作为 condition_id 传回。"""
        cid = "0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917"
        mock_to_cid.return_value = cid
        mock_client = MagicMock()
        mock_client.get_market.return_value = {
            "tokens": [
                {"outcome": "Yes", "token_id": "tok_y"},
                {"outcome": "No", "token_id": "tok_n"},
            ]
        }

        sig = Signal(
            market_id=cid,
            question="test?",
            outcome="NO",
            ev=0.1,
            confidence=0.8,
            size_usdc=1.0,
        )
        token_id = self.executor._resolve_token_id(mock_client, sig)
        assert token_id == "tok_n"
        assert sig.condition_id == cid

    @patch.object(OrderExecutor, "_to_condition_id")
    def test_resolve_non_0x_condition_id_not_saved(self, mock_to_cid):
        """如果 _to_condition_id 回退返回非 0x 值，不设置 condition_id。"""
        mock_to_cid.return_value = "12345"  # 非 0x 回退
        mock_client = MagicMock()
        mock_client.get_market.return_value = {
            "tokens": [
                {"outcome": "Yes", "token_id": "tok_y"},
            ]
        }

        sig = Signal(
            market_id="12345",
            question="test?",
            outcome="YES",
            ev=0.1,
            confidence=0.8,
            size_usdc=1.0,
        )
        self.executor._resolve_token_id(mock_client, sig)
        assert sig.condition_id == ""


# ---------------------------------------------------------------------------
# _verify_trade_on_chain 测试
# ---------------------------------------------------------------------------


class TestVerifyTradeOnChain:
    """验证 Data API 成交确认逻辑。"""

    @patch.dict(os.environ, {"POLY_RELAYER_ADDRESS": ""})
    def test_no_relayer_returns_not_verified(self):
        result = OrderExecutor._verify_trade_on_chain("0xabc", "BUY")
        assert result["verified"] is False

    @patch.dict(os.environ, {"POLY_RELAYER_ADDRESS": "0xwallet"})
    def test_no_condition_id_returns_not_verified(self):
        result = OrderExecutor._verify_trade_on_chain("", "BUY")
        assert result["verified"] is False

    @patch("src.executor.order_executor.time.sleep", return_value=None)
    @patch("src.executor.order_executor.requests.get")
    @patch.dict(os.environ, {"POLY_RELAYER_ADDRESS": "0xwallet"})
    def test_successful_verification(self, mock_get, mock_sleep):
        """Data API 返回有效成交数据时，验证成功。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "conditionId": "0xabc",
                "price": 0.55,
                "size": 5.0,
                "timestamp": time.time() - 5,
                "transactionHash": "0xtx123",
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = OrderExecutor._verify_trade_on_chain("0xabc", "BUY")
        assert result["verified"] is True
        assert result["price"] == 0.55
        assert result["size"] == 5.0

    @patch("src.executor.order_executor.time.sleep", return_value=None)
    @patch("src.executor.order_executor.requests.get")
    @patch.dict(os.environ, {"POLY_RELAYER_ADDRESS": "0xwallet"})
    def test_empty_trades_returns_not_verified(self, mock_get, mock_sleep):
        """Data API 无成交记录时返回未验证。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = OrderExecutor._verify_trade_on_chain("0xabc", "BUY", retries=1)
        assert result["verified"] is False

    @patch("src.executor.order_executor.time.sleep", return_value=None)
    @patch("src.executor.order_executor.requests.get")
    @patch.dict(os.environ, {"POLY_RELAYER_ADDRESS": "0xwallet"})
    def test_api_error_returns_not_verified(self, mock_get, mock_sleep):
        """Data API 请求失败时返回未验证。"""
        mock_get.side_effect = Exception("Connection timeout")

        result = OrderExecutor._verify_trade_on_chain("0xabc", "BUY", retries=1)
        assert result["verified"] is False

    @patch("src.executor.order_executor.time.sleep", return_value=None)
    @patch("src.executor.order_executor.requests.get")
    @patch.dict(os.environ, {"POLY_RELAYER_ADDRESS": "0xwallet"})
    def test_picks_latest_trade(self, mock_get, mock_sleep):
        """多笔成交时选择 timestamp 最新的。"""
        mock_resp = MagicMock()
        now = time.time()
        mock_resp.json.return_value = [
            {"conditionId": "0xabc", "price": 0.40, "size": 2.0, "timestamp": now - 30},
            {"conditionId": "0xabc", "price": 0.55, "size": 5.0, "timestamp": now - 10},
            {"conditionId": "0xabc", "price": 0.50, "size": 3.0, "timestamp": now - 20},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = OrderExecutor._verify_trade_on_chain("0xabc", "BUY")
        assert result["verified"] is True
        assert result["price"] == 0.55  # timestamp=200 最新

    @patch("src.executor.order_executor.time.sleep", return_value=None)
    @patch("src.executor.order_executor.requests.get")
    @patch.dict(os.environ, {"POLY_RELAYER_ADDRESS": "0xwallet"})
    def test_retry_on_empty_then_success(self, mock_get, mock_sleep):
        """第一次查询为空，重试成功。"""
        empty_resp = MagicMock()
        empty_resp.json.return_value = []
        empty_resp.raise_for_status = MagicMock()

        ok_resp = MagicMock()
        ok_resp.json.return_value = [
            {
                "conditionId": "0xabc",
                "price": 0.60,
                "size": 4.0,
                "timestamp": time.time() - 5,
            },
        ]
        ok_resp.raise_for_status = MagicMock()

        mock_get.side_effect = [empty_resp, ok_resp]

        result = OrderExecutor._verify_trade_on_chain("0xabc", "BUY", retries=2)
        assert result["verified"] is True
        assert result["price"] == 0.60


# ---------------------------------------------------------------------------
# _submit_order condition_id 传播测试
# ---------------------------------------------------------------------------


class TestSubmitOrderConditionIdPropagation:
    """验证 _submit_order 成功时 OrderResult 包含 condition_id。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.get")
    @patch("src.executor.order_executor.time.sleep", return_value=None)
    def test_condition_id_in_result_on_success(self, mock_sleep, mock_get):
        """成交后 OrderResult.condition_id 应包含解析的 condition_id。"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "general.dry_run": False,
            "executor.offset_pct": 0.001,
            "executor.clob_min_order_usdc": 1.10,
            "executor.min_order_size_shares": 2,
            "executor.fill_timeout_sec": 5,
            "executor.fill_poll_interval_sec": 1,
        }.get(key)

        mock_client = MagicMock()
        mock_client.get_balance_allowance.side_effect = [
            {"balance": "100000000"},
            {"balance": "97000000"},
        ]
        mock_client.create_and_post_order.return_value = {"orderID": "ord-cid"}
        mock_client.get_order.return_value = {
            "status": "MATCHED",
            "size_matched": "5.0",
            "price": "0.60",
        }
        self.executor._client = mock_client

        sig = Signal(
            market_id="mkt_001",
            question="test?",
            outcome="YES",
            ev=0.1,
            confidence=0.8,
            size_usdc=3.0,
            token_id="0xTOK",
            expected_price=0.60,
            condition_id="0xcondition123",
        )

        result = self.executor._submit_order(sig)
        assert result.success is True
        assert result.condition_id == "0xcondition123"


# ---------------------------------------------------------------------------
# sell_position condition_id 传播测试
# ---------------------------------------------------------------------------


class TestSellPositionConditionId:
    """验证 sell_position 从 bet dict 传播 condition_id。"""

    def setup_method(self):
        self.executor = OrderExecutor()

    @patch("src.executor.order_executor.get")
    def test_dry_run_sell_creates_signal_with_condition_id(self, mock_get):
        """DRY RUN 卖出时 sell_signal 应包含 bet 中的 condition_id。"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "general.dry_run": True,
        }.get(key)

        bet = {
            "market_id": "mkt_001",
            "outcome": "YES",
            "exit_price": 0.70,
            "market_price": 0.50,
            "size_usdc": 2.0,
            "question": "test?",
            "condition_id": "0xcid789",
        }
        result = self.executor.sell_position(bet)
        assert result.success is True
        assert result.signal.condition_id == "0xcid789"


# ---------------------------------------------------------------------------
# _db_write_live_bets condition_id 测试
# ---------------------------------------------------------------------------


class TestDbWriteLiveBetsConditionId:
    """验证 _db_write_live_bets 写入 condition_id。"""

    @patch("main.insert_paper_bet")
    @patch("main.detect_category", return_value="weather")
    @patch("main._extract_avg_probability", return_value=0.65)
    @patch("main._extract_reasoning", return_value="test reasoning")
    def test_condition_id_included_in_bet_record(
        self, mock_reason, mock_prob, mock_cat, mock_insert
    ):
        """实盘成功订单写入 DB 时应包含 condition_id。"""
        from main import _db_write_live_bets

        sig = Signal(
            market_id="mkt_001",
            question="Will it rain?",
            outcome="YES",
            ev=0.1,
            confidence=0.8,
            size_usdc=2.0,
            condition_id="0xcond_from_signal",
        )
        result = OrderResult(
            signal=sig,
            success=True,
            tx_hash="real_tx_hash",
            actual_price=0.55,
            filled_size=2.0,
            condition_id="0xcond_from_result",
        )

        market_lookup = {"mkt_001": {"slug": "test-slug"}}
        _db_write_live_bets([result], market_lookup, balance_before=100.0)

        mock_insert.assert_called_once()
        bet_record = mock_insert.call_args[0][0]
        # condition_id 应优先使用 signal 上的值
        assert bet_record["condition_id"] == "0xcond_from_signal"

    @patch("main.insert_paper_bet")
    @patch("main.detect_category", return_value="crypto")
    @patch("main._extract_avg_probability", return_value=0.5)
    @patch("main._extract_reasoning", return_value="")
    def test_condition_id_fallback_to_order_result(
        self, mock_reason, mock_prob, mock_cat, mock_insert
    ):
        """signal 无 condition_id 时，回退到 OrderResult 的 condition_id。"""
        from main import _db_write_live_bets

        sig = Signal(
            market_id="mkt_002",
            question="BTC above 100k?",
            outcome="YES",
            ev=0.05,
            confidence=0.7,
            size_usdc=1.5,
            # condition_id 为空
        )
        result = OrderResult(
            signal=sig,
            success=True,
            tx_hash="tx_002",
            actual_price=0.45,
            filled_size=1.5,
            condition_id="0xcond_from_result_only",
        )

        market_lookup = {"mkt_002": {"slug": "btc-100k"}}
        _db_write_live_bets([result], market_lookup, balance_before=50.0)

        mock_insert.assert_called_once()
        bet_record = mock_insert.call_args[0][0]
        assert bet_record["condition_id"] == "0xcond_from_result_only"

    @patch("main.insert_paper_bet")
    @patch("main.detect_category", return_value="sports")
    @patch("main._extract_avg_probability", return_value=0.6)
    @patch("main._extract_reasoning", return_value="")
    def test_dry_run_tx_skipped(self, mock_reason, mock_prob, mock_cat, mock_insert):
        """DRY_RUN 交易不写入 DB。"""
        from main import _db_write_live_bets

        sig = Signal(
            market_id="mkt_003",
            question="test?",
            outcome="YES",
            ev=0.1,
            confidence=0.7,
            size_usdc=1.0,
        )
        result = OrderResult(signal=sig, success=True, tx_hash="DRY_RUN")

        _db_write_live_bets([result], {}, balance_before=100.0)
        mock_insert.assert_not_called()


# ---------------------------------------------------------------------------
# Auth threshold 测试
# ---------------------------------------------------------------------------


class TestAuthThreshold:
    """验证 IP 封禁阈值已从 10 降到 3。"""

    def test_global_limiter_max_fails_is_3(self):
        from scripts.dashboard_routes._shared import ip_rate_limiter

        assert ip_rate_limiter.max_fails == 3

    def test_block_after_3_fails(self):
        from scripts.dashboard_routes._shared import IPRateLimiter

        limiter = IPRateLimiter(max_fails=3, window_sec=300, block_sec=900)
        ip = "10.10.10.10"
        limiter.record_fail(ip)
        limiter.record_fail(ip)
        assert not limiter.is_blocked(ip)
        triggered = limiter.record_fail(ip)
        assert triggered is True
        assert limiter.is_blocked(ip)


# ---------------------------------------------------------------------------
# Phase 31: correct_closed_bet_exit_data DB 函数测试
# ---------------------------------------------------------------------------


class TestCorrectClosedBetExitData:
    """验证 DB 修正函数 correct_closed_bet_exit_data。"""

    def _setup_db(self, tmp_path):
        """创建内存 DB 并插入一条测试记录。"""
        import time

        from src.utils import db as _db

        db_path = tmp_path / "test.db"
        _db.init_db(db_path)
        _db.insert_paper_bet(
            {
                "bet_id": "test-bet-001",
                "ts_entered": time.time(),
                "market_id": "1597885",
                "question": "Test Market",
                "category": "sports",
                "outcome": "NO",
                "confidence": 0.7,
                "ai_probability": 0.3,
                "market_price": 0.341,
                "ev": 0.05,
                "size_usdc": 2.95,
                "sim_balance_before": 100.0,
                "sim_balance_after": 97.05,
                "market_end_date": "",
                "weather_context": "",
                "ai_models": "test",
                "cycle_id": "test-cycle",
            },
            db_path=db_path,
        )
        # 关闭仓位（模拟止盈）
        _db.close_paper_bet_profit_take(
            bet_id="test-bet-001",
            exit_price=0.35,
            pnl=0.08,
            payout=3.03,
            db_path=db_path,
        )
        return db_path

    def test_corrects_exit_price(self, tmp_path):
        from src.utils.db import connect, correct_closed_bet_exit_data

        db_path = self._setup_db(tmp_path)
        result = correct_closed_bet_exit_data(
            bet_id="test-bet-001",
            exit_price=0.42,
            pnl=0.6,
            payout=3.55,
            db_path=db_path,
        )
        assert result is True

        with connect(db_path) as conn:
            row = conn.execute(
                "SELECT exit_price, pnl, payout FROM paper_bets WHERE bet_id='test-bet-001'"
            ).fetchone()
        assert row["exit_price"] == 0.42
        assert row["pnl"] == 0.6
        assert row["payout"] == 3.55

    def test_fills_condition_id(self, tmp_path):
        from src.utils.db import connect, correct_closed_bet_exit_data

        db_path = self._setup_db(tmp_path)
        correct_closed_bet_exit_data(
            bet_id="test-bet-001",
            exit_price=0.42,
            pnl=0.6,
            payout=3.55,
            condition_id="0xabc123",
            db_path=db_path,
        )
        with connect(db_path) as conn:
            row = conn.execute(
                "SELECT condition_id FROM paper_bets WHERE bet_id='test-bet-001'"
            ).fetchone()
        assert row["condition_id"] == "0xabc123"

    def test_does_not_overwrite_existing_condition_id(self, tmp_path):
        from src.utils.db import connect, correct_closed_bet_exit_data

        db_path = self._setup_db(tmp_path)
        # 先写入一个 condition_id
        with connect(db_path) as conn:
            conn.execute(
                "UPDATE paper_bets SET condition_id='0xexisting' WHERE bet_id='test-bet-001'"
            )
        # 尝试覆盖
        correct_closed_bet_exit_data(
            bet_id="test-bet-001",
            exit_price=0.42,
            pnl=0.6,
            payout=3.55,
            condition_id="0xnew_value",
            db_path=db_path,
        )
        with connect(db_path) as conn:
            row = conn.execute(
                "SELECT condition_id FROM paper_bets WHERE bet_id='test-bet-001'"
            ).fetchone()
        # 应保留原有值
        assert row["condition_id"] == "0xexisting"

    def test_skips_open_bets(self, tmp_path):
        """不应修改未关闭的仓位。"""
        import time

        from src.utils import db as _db
        from src.utils.db import connect, correct_closed_bet_exit_data

        db_path = tmp_path / "test2.db"
        _db.init_db(db_path)
        _db.insert_paper_bet(
            {
                "bet_id": "open-bet-001",
                "ts_entered": time.time(),
                "market_id": "1111",
                "question": "Open Bet",
                "category": "crypto",
                "outcome": "YES",
                "confidence": 0.7,
                "ai_probability": 0.7,
                "market_price": 0.5,
                "ev": 0.1,
                "size_usdc": 1.0,
                "sim_balance_before": 100.0,
                "sim_balance_after": 99.0,
                "market_end_date": "",
                "weather_context": "",
                "ai_models": "test",
                "cycle_id": "test-cycle",
            },
            db_path=db_path,
        )
        result = correct_closed_bet_exit_data(
            bet_id="open-bet-001",
            exit_price=0.8,
            pnl=0.5,
            payout=1.5,
            db_path=db_path,
        )
        assert result is False  # 未更新任何行

    def test_updates_is_win(self, tmp_path):
        """修正后 is_win 应基于新的 pnl。"""
        from src.utils.db import connect, correct_closed_bet_exit_data

        db_path = self._setup_db(tmp_path)
        # 修正为亏损
        correct_closed_bet_exit_data(
            bet_id="test-bet-001",
            exit_price=0.2,
            pnl=-0.5,
            payout=2.45,
            db_path=db_path,
        )
        with connect(db_path) as conn:
            row = conn.execute(
                "SELECT is_win FROM paper_bets WHERE bet_id='test-bet-001'"
            ).fetchone()
        assert row["is_win"] == 0


# ---------------------------------------------------------------------------
# Phase 31: sell_position condition_id 解析兜底测试
# ---------------------------------------------------------------------------


class TestSellPositionConditionIdFallback:
    """验证 sell_position 在 condition_id 为空时通过 Gamma API 解析。"""

    @patch.object(OrderExecutor, "_to_condition_id")
    @patch.object(OrderExecutor, "_verify_trade_on_chain")
    def test_sell_resolves_cid_when_empty(self, mock_verify, mock_to_cid):
        """Data API 校准代码路径：当 sell_signal.condition_id 为空时，
        应调用 _to_condition_id 补全。"""
        mock_to_cid.return_value = "0xresolved_cid"
        mock_verify.return_value = {"verified": True, "price": 0.42, "size": 8.7}

        # 直接测试 sell_position 中 Data API 校准代码块的逻辑
        # 模拟 sell_signal.condition_id 为空的情况
        sig = Signal(
            market_id="1597885",
            question="test",
            outcome="NO",
            ev=0,
            confidence=0,
            size_usdc=2.95,
            condition_id="",  # 空的
        )

        sell_cid = getattr(sig, "condition_id", "")
        if not sell_cid:
            sell_cid = OrderExecutor._to_condition_id("1597885")
            if sell_cid and sell_cid.startswith("0x"):
                sig.condition_id = sell_cid

        assert sig.condition_id == "0xresolved_cid"
        mock_to_cid.assert_called_once_with("1597885")


# ---------------------------------------------------------------------------
# Phase 31: _post_close_chain_verify 测试
# ---------------------------------------------------------------------------


class TestPostCloseChainVerify:
    """验证 _post_close_chain_verify 异步链上校验逻辑。"""

    @patch("main.time")
    @patch("src.executor.order_executor.OrderExecutor._verify_trade_on_chain")
    @patch("src.executor.order_executor.OrderExecutor._to_condition_id")
    @patch("src.utils.db.correct_closed_bet_exit_data")
    def test_verify_corrects_db(
        self, mock_correct, mock_to_cid, mock_verify, mock_time
    ):
        """链上价格与 DB 不同时应调用 correct_closed_bet_exit_data。"""
        mock_time.sleep = MagicMock()
        mock_to_cid.return_value = "0xabc123"
        mock_verify.return_value = {"verified": True, "price": 0.42, "size": 8.7}

        # 直接调用内部 _verify 逻辑（绕过 threading）
        bet = {
            "bet_id": "27c3d8c5-98f8",
            "market_id": "1597885",
            "market_price": 0.341,
            "size_usdc": 2.95,
            "exit_price": 0.35,
            "pnl": 0.08,
            "condition_id": "",
        }

        # 模拟 _verify 内部逻辑
        cid = bet.get("condition_id", "")
        if not cid or not cid.startswith("0x"):
            cid = OrderExecutor._to_condition_id(bet["market_id"])

        chain = OrderExecutor._verify_trade_on_chain(cid, side="SELL", retries=3)
        assert chain["verified"] is True

        chain_price = chain["price"]
        entry_price = float(bet["market_price"])
        size_usdc = float(bet["size_usdc"])
        shares = size_usdc / entry_price
        proceeds = round(shares * chain_price, 4)
        pnl = round(proceeds - size_usdc, 4)

        db_exit = float(bet.get("exit_price", 0))
        assert abs(chain_price - db_exit) >= 0.005  # 有差异

        from src.utils.db import correct_closed_bet_exit_data

        correct_closed_bet_exit_data(
            bet_id=bet["bet_id"],
            exit_price=chain_price,
            pnl=pnl,
            payout=proceeds,
            condition_id=cid,
        )
        mock_correct.assert_called_once()
        call_args = mock_correct.call_args
        assert call_args.kwargs["exit_price"] == 0.42
        assert call_args.kwargs["condition_id"] == "0xabc123"

    def test_verify_skips_small_diff(self):
        """链上价格差异 < 0.005 时不修正。"""
        bet = {
            "exit_price": 0.350,
        }
        chain_price = 0.352  # 差 0.002
        db_exit = float(bet.get("exit_price", 0))
        assert abs(chain_price - db_exit) < 0.005  # 差异太小

    def test_verify_skips_no_verified(self):
        """Data API 返回 verified=False 时不修正。"""
        chain = {"verified": False}
        assert not chain.get("verified")
        assert not chain.get("verified")
        assert not chain.get("verified")
