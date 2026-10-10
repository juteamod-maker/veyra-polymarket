"""T35: 测试 _fetch_manual_sell_data / _close_dust_residual / _close_zombie 改进。

验证场景：
1. Data API 返回正常交易 → 使用真实成交价格
2. Data API 无交易记录 → 回退到入场价
3. Data API 异常/超时 → 回退到入场价
4. 缺少 condition_id 或 funder → 回退到入场价
5. _close_dust_residual_position 使用真实价格关闭仓位
6. _close_zombie_position 使用真实价格关闭仓位
"""

import os
from unittest.mock import MagicMock, patch

import pytest

# ── 被测函数导入 ──────────────────────────────────────────────
from main import (
    _close_dust_residual_position,
    _close_zombie_position,
    _fetch_manual_sell_data,
)

# ── fixtures ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _env_funder(monkeypatch):
    """设置 POLY_RELAYER_ADDRESS 环境变量。"""
    monkeypatch.setenv(
        "POLY_RELAYER_ADDRESS", "0x822e0247C3fE0cCb38C624C94Ed96bCa5bfEd8DD"
    )


CONDITION_ID = "0xabc123"
ENTRY_PRICE = 0.78
MATCHED_SHARES = 6.41  # size / entry_price


# ── _fetch_manual_sell_data 测试 ──────────────────────────────


class TestFetchManualSellData:
    """测试 Data API 查询真实卖出数据的函数。"""

    @patch("main.requests.get")
    def test_api_returns_trades(self, mock_get):
        """Data API 返回 SELL 交易 → 使用真实加权平均价格。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"size": 3.0, "price": 0.82},
            {"size": 3.41, "price": 0.84},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _fetch_manual_sell_data(CONDITION_ID, ENTRY_PRICE, MATCHED_SHARES)

        assert result["source"] == "data_api"
        # 加权平均: (3.0*0.82 + 3.41*0.84) / (3.0+3.41) = (2.46+2.8644)/6.41 = 0.83038
        assert 0.82 < result["exit_price"] < 0.85
        # payout = 3.0*0.82 + 3.41*0.84 = 5.3244
        assert result["payout"] > 0

        # 验证 API 调用参数
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        params = (
            call_kwargs[1]["params"]
            if "params" in call_kwargs[1]
            else (
                call_kwargs[0][1]
                if len(call_kwargs[0]) > 1
                else call_kwargs.kwargs.get("params")
            )
        )
        assert params["side"] == "SELL"
        assert params["market"] == CONDITION_ID

    @patch("main.requests.get")
    def test_api_no_trades(self, mock_get):
        """Data API 返回空列表 → 回退到入场价。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _fetch_manual_sell_data(CONDITION_ID, ENTRY_PRICE, MATCHED_SHARES)

        assert result["source"] == "entry_price_fallback"
        assert result["exit_price"] == ENTRY_PRICE
        assert abs(result["payout"] - round(MATCHED_SHARES * ENTRY_PRICE, 4)) < 0.01

    @patch("main.requests.get")
    def test_api_timeout(self, mock_get):
        """Data API 超时 → 回退到入场价。"""
        import requests as req

        mock_get.side_effect = req.exceptions.Timeout("connect timeout")

        result = _fetch_manual_sell_data(CONDITION_ID, ENTRY_PRICE, MATCHED_SHARES)

        assert result["source"] == "entry_price_fallback"
        assert result["exit_price"] == ENTRY_PRICE

    @patch("main.requests.get")
    def test_api_500_error(self, mock_get):
        """Data API 返回 500 → 回退到入场价。"""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("500 Server Error")
        mock_get.return_value = mock_resp

        result = _fetch_manual_sell_data(CONDITION_ID, ENTRY_PRICE, MATCHED_SHARES)

        assert result["source"] == "entry_price_fallback"
        assert result["exit_price"] == ENTRY_PRICE

    def test_missing_condition_id(self):
        """缺少 condition_id → 直接回退。"""
        result = _fetch_manual_sell_data("", ENTRY_PRICE, MATCHED_SHARES)

        assert result["source"] == "entry_price_fallback"
        assert result["exit_price"] == ENTRY_PRICE

    def test_missing_funder(self, monkeypatch):
        """缺少 POLY_RELAYER_ADDRESS → 直接回退。"""
        monkeypatch.delenv("POLY_RELAYER_ADDRESS", raising=False)

        result = _fetch_manual_sell_data(CONDITION_ID, ENTRY_PRICE, MATCHED_SHARES)

        assert result["source"] == "entry_price_fallback"
        assert result["exit_price"] == ENTRY_PRICE

    @patch("main.requests.get")
    def test_api_returns_non_list(self, mock_get):
        """Data API 返回非列表 → 回退到入场价。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"error": "not found"}
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _fetch_manual_sell_data(CONDITION_ID, ENTRY_PRICE, MATCHED_SHARES)

        assert result["source"] == "entry_price_fallback"

    @patch("main.requests.get")
    def test_single_trade(self, mock_get):
        """Data API 返回单笔交易（最常见场景）。"""
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"size": 6.61, "price": 0.83}]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        result = _fetch_manual_sell_data(CONDITION_ID, ENTRY_PRICE, MATCHED_SHARES)

        assert result["source"] == "data_api"
        assert result["exit_price"] == 0.83
        assert abs(result["payout"] - 5.4863) < 0.01


# ── _close_dust_residual_position 测试 ────────────────────────


class TestCloseDustResidualPosition:
    """测试 dust 关闭函数使用 Data API 真实价格。"""

    @patch("main.close_paper_bet_manual_exit")
    @patch("main._fetch_manual_sell_data")
    def test_uses_real_price(self, mock_fetch, mock_close):
        """dust 关闭应使用 Data API 真实价格。"""
        mock_fetch.return_value = {
            "exit_price": 0.83,
            "payout": 5.4863,
            "source": "data_api",
        }
        bet = {
            "bet_id": "006abf77-test",
            "market_price": 0.78,
            "exit_price": 0.999,  # 错误的 trailing_peak — 应被忽略
            "size_usdc": 5.16,
            "condition_id": CONDITION_ID,
        }

        _close_dust_residual_position(bet, "profit_take_dust")

        mock_close.assert_called_once()
        call_kwargs = mock_close.call_args[1]
        assert call_kwargs["exit_price"] == 0.83
        assert call_kwargs["payout"] == 5.4863
        # pnl = 5.4863 - 5.16 = +0.3263
        assert call_kwargs["pnl"] > 0
        assert call_kwargs["pnl"] < 1.0  # 不是虚报的 +1.44

    @patch("main.close_paper_bet_manual_exit")
    @patch("main._fetch_manual_sell_data")
    def test_fallback_to_entry_price(self, mock_fetch, mock_close):
        """Data API 失败时以入场价回退 → PnL ≈ 0。"""
        mock_fetch.return_value = {
            "exit_price": 0.78,
            "payout": 5.16,
            "source": "entry_price_fallback",
        }
        bet = {
            "bet_id": "006abf77-test",
            "market_price": 0.78,
            "exit_price": 0.999,
            "size_usdc": 5.16,
            "condition_id": CONDITION_ID,
        }

        _close_dust_residual_position(bet, "stop_loss_dust")

        mock_close.assert_called_once()
        call_kwargs = mock_close.call_args[1]
        assert call_kwargs["exit_price"] == 0.78
        # pnl = 5.16 - 5.16 = 0
        assert abs(call_kwargs["pnl"]) < 0.01


# ── _close_zombie_position 测试 ───────────────────────────────


class TestCloseZombiePosition:
    """测试 zombie 关闭函数使用 Data API 真实价格。"""

    @patch("main.close_paper_bet_manual_exit")
    @patch("main._fetch_manual_sell_data")
    def test_uses_real_price(self, mock_fetch, mock_close):
        """zombie 关闭应使用 Data API 真实价格。"""
        mock_fetch.return_value = {
            "exit_price": 0.75,
            "payout": 4.8,
            "source": "data_api",
        }
        bet = {
            "bet_id": "test-zombie-01",
            "market_price": 0.80,
            "exit_price": 0.95,
            "size_usdc": 5.0,
            "condition_id": CONDITION_ID,
        }

        _close_zombie_position(bet, "stop_loss")

        mock_close.assert_called_once()
        call_kwargs = mock_close.call_args[1]
        assert call_kwargs["exit_price"] == 0.75
        assert call_kwargs["payout"] == 4.8
        # pnl = 4.8 - 5.0 = -0.2
        assert call_kwargs["pnl"] == -0.2

    @patch("main.close_paper_bet_manual_exit")
    @patch("main._fetch_manual_sell_data")
    def test_fallback_uses_entry_price(self, mock_fetch, mock_close):
        """zombie API 失败 → 入场价回退。"""
        mock_fetch.return_value = {
            "exit_price": 0.80,
            "payout": 5.0,
            "source": "entry_price_fallback",
        }
        bet = {
            "bet_id": "test-zombie-02",
            "market_price": 0.80,
            "exit_price": 0.95,  # trailing peak — 应被忽略
            "size_usdc": 5.0,
            "condition_id": CONDITION_ID,
        }

        _close_zombie_position(bet, "profit_take")

        call_kwargs = mock_close.call_args[1]
        assert call_kwargs["exit_price"] == 0.80
        assert abs(call_kwargs["pnl"]) < 0.01  # pnl ≈ 0
