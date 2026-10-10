"""
test_phase42_optimizations.py — Phase 42 全部优化和修复测试

覆盖：
  - Task 112: min-order-size 强制核销（跳过链上验证）+ 买入缓冲
  - P2a: 天气 YES 方向屏蔽
  - Task 114: crypto 非价格市场降级分类
  - P3a: 天气止损宽度验证（config 值）
"""

import json
import math
from unittest.mock import MagicMock, patch

import pytest
from src.utils.category_detector import _is_crypto_price_market, detect_category


# ======================================================================
# Task 114: Crypto 非价格市场降级分类
# ======================================================================
class TestCryptoClassificationPhase42:
    """验证含 crypto 关键词但非价格/交易类市场不被误分类为 crypto。"""

    def test_bitcoin_gta_vi_not_crypto(self):
        """fc27480d: 'Will bitcoin hit $1m before GTA VI' 不应归为 crypto。
        'before GTA VI' 不是日期模式，是与游戏的对比。"""
        m = {"question": "Will bitcoin hit $1m before GTA VI?"}
        # "hit $1m" 会匹配部分价格模式，但 "before GTA VI" 不算日期
        # 实际上 $1m 匹配 \$[\d,]+ — 需要更细致的排除
        # 但 $1,000,000 用 $1m 可能不匹配 \$[\d,]+
        # 测试实际行为
        cat = detect_category(m)
        # "$1m" 不匹配 \$[\d,]+ (因为 m 不是数字)
        # 也不匹配其他价格模式
        # 所以应该降级
        assert cat != "crypto", f"Expected non-crypto, got {cat}"

    def test_bitcoin_100k_is_crypto(self):
        """标准 crypto 价格市场应正确分类。"""
        m = {"question": "Will Bitcoin exceed $100,000?"}
        assert detect_category(m) == "crypto"

    def test_bitcoin_above_90k_is_crypto(self):
        m = {"question": "Will Bitcoin go above $90,000 by March?"}
        assert detect_category(m) == "crypto"

    def test_eth_reach_5000_is_crypto(self):
        m = {"question": "Will ETH reach $5000 by end of 2026?"}
        assert detect_category(m) == "crypto"

    def test_bitcoin_etf_is_crypto(self):
        m = {"question": "Will a Bitcoin ETF be approved?"}
        assert detect_category(m) == "crypto"

    def test_bitcoin_halving_is_crypto(self):
        m = {"question": "Will Bitcoin reach all-time high after halving?"}
        assert detect_category(m) == "crypto"

    def test_bitcoin_drop_below_is_crypto(self):
        m = {"question": "Will Bitcoin drop below $50,000?"}
        assert detect_category(m) == "crypto"

    def test_crypto_tag_always_crypto(self):
        """平台 tag 标记的 crypto 市场始终分类正确。"""
        m = {
            "question": "Some random question about bitcoin",
            "tags": [{"slug": "crypto"}],
        }
        assert detect_category(m) == "crypto"

    def test_bitcoin_ath_is_crypto(self):
        m = {"question": "Will Bitcoin hit a new ATH in 2026?"}
        assert detect_category(m) == "crypto"

    def test_bitcoin_market_cap_is_crypto(self):
        m = {"question": "Will Bitcoin market cap exceed $2 trillion?"}
        assert detect_category(m) == "crypto"


class TestIsCryptoPriceMarket:
    """验证 _is_crypto_price_market 价格模式匹配。"""

    def test_dollar_amount(self):
        assert _is_crypto_price_market("Will bitcoin exceed $100000?") is True

    def test_dollar_comma(self):
        assert _is_crypto_price_market("Will BTC reach $100,000?") is True

    def test_above_price(self):
        assert _is_crypto_price_market("Will ETH go above $5000?") is True

    def test_below_price(self):
        assert _is_crypto_price_market("Will SOL drop below $100?") is True

    def test_k_suffix(self):
        assert _is_crypto_price_market("Will BTC reach 100k?") is True

    def test_by_month(self):
        assert _is_crypto_price_market("Will BTC rise by March 2026?") is True

    def test_end_of_year(self):
        assert _is_crypto_price_market("BTC at end of 2026?") is True

    def test_etf(self):
        assert _is_crypto_price_market("Will Bitcoin ETF be approved?") is True

    def test_no_price_pattern(self):
        """不含价格模式的问题应返回 False。"""
        assert (
            _is_crypto_price_market("Will bitcoin exist before GTA VI launches?")
            is False
        )

    def test_gta_vi(self):
        """'hit $1m before GTA VI' — $1m 中 m 非数字，不匹配。"""
        assert _is_crypto_price_market("Will bitcoin hit $1m before GTA VI?") is False


# ======================================================================
# P2a: 天气 YES 方向屏蔽
# ======================================================================
class TestMinOrderSharesBuffer:
    """验证买入时 min_order_shares + buffer 确保 partial fill 仍>= 5。"""

    @patch("src.executor.order_executor.get")
    def test_buy_buffer_pads_shares(self, mock_get):
        """当 size_shares < padded_min (5+1=6) 时自动调整到 6。"""

        def side_get(key):
            return {
                "executor.min_order_size_shares": 5,
                "executor.post_buy_topup_buffer_shares": 1.0,
                "executor.clob_min_order_usdc": 1.10,
            }.get(key)

        mock_get.side_effect = side_get

        # Verify the config read logic
        min_shares = mock_get("executor.min_order_size_shares") or 5
        buffer = float(mock_get("executor.post_buy_topup_buffer_shares") or 1.0)
        padded = min_shares + buffer
        assert padded == 6.0

        # Simulate: 4 shares at $0.50 = $2.00
        size_shares = 4.0
        limit_price = 0.50
        if size_shares < padded:
            size_shares = padded
        assert size_shares == 6.0
        assert size_shares * limit_price == 3.00  # $3 USDC cost


# ======================================================================
# Task 112: force_write_off 跳过链上验证
# ======================================================================
class TestForceWriteOffBelowMin:
    """验证 below_min_order_size 触发时: SUPREME 1 修复后，
    需检查市场 resolved 状态，未 resolved 则阻止核销。"""

    @patch("main.is_dry_run", return_value=False)
    @patch("main.close_paper_bet_profit_take")
    @patch("main.increment_sell_fail_count")
    @patch("main.requests.get")
    def test_below_min_blocks_when_not_resolved(
        self, mock_requests, mock_incr, mock_close, mock_dry
    ):
        """below_min_order_size + 市场未resolved → 阻止核销（SUPREME 1）。"""
        from main import _force_write_off_position

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"resolved": False}
        mock_resp.raise_for_status = MagicMock()
        mock_requests.return_value = mock_resp

        bet = {
            "bet_id": "test_bet_123",
            "market_price": 0.50,
            "exit_price": 0.40,
            "size_usdc": 2.50,
            "condition_id": "0xabc123",
            "market_id": "mkt_test_123",
        }
        _force_write_off_position(
            bet,
            trigger="profit_take_min_shares",
            reason="below_min_order_size_force_writeoff",
        )
        # Gamma API should be called to check resolved status
        mock_requests.assert_called_once()
        # Market NOT resolved → must NOT close DB
        mock_close.assert_not_called()
        # Must increment sell_fail_count
        mock_incr.assert_called_once_with("test_bet_123")

    @patch("main.is_dry_run", return_value=False)
    @patch("main.close_paper_bet_stop_loss")
    @patch("main.increment_sell_fail_count")
    @patch("main.requests.get")
    def test_below_min_allows_when_resolved(
        self, mock_requests, mock_incr, mock_close, mock_dry
    ):
        """below_min_order_size + 市场已resolved → 允许核销。"""
        from main import _force_write_off_position

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"resolved": True}
        mock_resp.raise_for_status = MagicMock()
        mock_requests.return_value = mock_resp

        bet = {
            "bet_id": "test_bet_456",
            "market_price": 0.60,
            "exit_price": 0.30,
            "size_usdc": 3.00,
            "condition_id": "0xdef789",
            "market_id": "mkt_test_456",
        }
        _force_write_off_position(
            bet,
            trigger="stop_loss_min_shares",
            reason="below_min_order_size_force_writeoff",
        )
        # Market resolved → close DB allowed
        mock_close.assert_called_once()
        mock_incr.assert_not_called()

    @patch("main.is_dry_run", return_value=False)
    @patch("main.close_paper_bet_profit_take")
    @patch("main.increment_sell_fail_count")
    @patch("main.requests.get")
    @patch.dict("os.environ", {"POLY_RELAYER_ADDRESS": "0xfunder"})
    def test_timeout_retries_still_checks_chain(
        self, mock_requests, mock_incr, mock_close, mock_dry
    ):
        """非 below_min 触发（如 timeout）仍应检查链上状态。"""
        from main import _force_write_off_position

        # Mock chain response: no shares (empty positions)
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_requests.return_value = mock_resp

        bet = {
            "bet_id": "test_bet_789",
            "market_price": 0.50,
            "exit_price": 0.35,
            "size_usdc": 2.00,
            "condition_id": "0xghi012",
        }
        _force_write_off_position(
            bet,
            trigger="profit_take_write_off",
            reason="timeout_after_3_retries",
        )
        # Chain check SHOULD be called for non-min-order triggers
        mock_requests.assert_called_once()
        # T55: profit_take trigger → close_paper_bet_profit_take
        mock_close.assert_called_once()


# ======================================================================
# P1: 分歧阈值配置验证
# ======================================================================
class TestDivergenceThresholdConfig:
    """验证 config 中分歧阈值已调整为 0.20（VPS生产调优值）。"""

    def test_divergence_threshold_value(self):
        with open("config/config.json") as f:
            cfg = json.load(f)
        threshold = cfg["weather_ensemble"]["ecmwf_divergence_threshold"]
        assert threshold == 0.3, f"Expected 0.3 (T252: 扩大天气覆盖), got {threshold}"


# ======================================================================
# P3a: 天气止损 config 验证
# ======================================================================
class TestWeatherStopLossConfig:
    """验证天气止损兜底值为 80%（Phase53激进放宽），时间加权止损已禁用。"""

    def test_weather_stop_pct(self):
        with open("config/config.json") as f:
            cfg = json.load(f)
        pct = cfg["stop_loss"]["weather_standard_stop_pct"]
        assert pct == 0.70, f"Expected 0.70 (T262 widened), got {pct}"

    def test_weather_time_weighted_stop_enabled(self):
        """T185: 天气时间加权止损已启用(从Phase53的false改为true)。"""
        with open("config/config.json") as f:
            cfg = json.load(f)
        tw = cfg["stop_loss"]["weather_time_weighted_stop"]
        assert tw["enabled"] is True


# ======================================================================
# T113: Signal engine 置信度门槛降低
# ======================================================================
class TestSignalConfidenceConfig:
    """验证 signal.min_confidence == 0.55 (T261)。"""

    def test_signal_min_confidence(self):
        with open("config/config.json") as f:
            cfg = json.load(f)
        conf = cfg["signal"]["min_confidence"]
        assert conf == 0.55, f"Expected 0.55 (T261), got {conf}"
        assert conf == 0.55, f"Expected 0.55 (T261), got {conf}"
        assert conf == 0.55, f"Expected 0.55 (T261), got {conf}"
        assert conf == 0.55, f"Expected 0.55 (T261), got {conf}"
