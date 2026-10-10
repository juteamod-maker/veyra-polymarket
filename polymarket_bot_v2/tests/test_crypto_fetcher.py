"""tests/test_crypto_fetcher.py — crypto_fetcher 单元测试"""

from unittest.mock import MagicMock, patch

import pytest

# ------------------------------------------------------------------
# _extract_coin 测试
# ------------------------------------------------------------------


class TestExtractCoin:
    """从 question 中解析 CoinGecko coin ID。"""

    def test_bitcoin_keyword(self):
        from src.utils.crypto_fetcher import _extract_coin

        assert _extract_coin("Will the price of Bitcoin be above $70,000?") == "bitcoin"

    def test_btc_keyword(self):
        from src.utils.crypto_fetcher import _extract_coin

        assert _extract_coin("Will BTC reach $100,000?") == "bitcoin"

    def test_ethereum_keyword(self):
        from src.utils.crypto_fetcher import _extract_coin

        assert _extract_coin("Will Ethereum be above $2,200 on March 16?") == "ethereum"

    def test_eth_keyword(self):
        from src.utils.crypto_fetcher import _extract_coin

        assert _extract_coin("Will ETH price drop below $1,800?") == "ethereum"

    def test_solana_keyword(self):
        from src.utils.crypto_fetcher import _extract_coin

        assert _extract_coin("Will Solana reach $200?") == "solana"

    def test_unknown_coin_returns_none(self):
        from src.utils.crypto_fetcher import _extract_coin

        assert _extract_coin("Will gold reach $3,000 per ounce?") is None

    def test_case_insensitive(self):
        from src.utils.crypto_fetcher import _extract_coin

        assert _extract_coin("BITCOIN price prediction") == "bitcoin"


# ------------------------------------------------------------------
# _extract_target_price 测试
# ------------------------------------------------------------------


class TestExtractTargetPrice:
    """从 question 中解析目标价格。"""

    def test_standard_price(self):
        from src.utils.crypto_fetcher import _extract_target_price

        assert _extract_target_price("Bitcoin above $70,000") == 70000.0

    def test_price_with_decimals(self):
        from src.utils.crypto_fetcher import _extract_target_price

        assert _extract_target_price("ETH above $2,200.50") == 2200.50

    def test_multiple_prices_takes_max(self):
        from src.utils.crypto_fetcher import _extract_target_price

        # "between $70,000 and $72,000" → max = 72000
        result = _extract_target_price("Will Bitcoin be between $70,000 and $72,000?")
        assert result == 72000.0

    def test_no_price_returns_none(self):
        from src.utils.crypto_fetcher import _extract_target_price

        assert _extract_target_price("Will Bitcoin crash?") is None


# ------------------------------------------------------------------
# _build_context 测试
# ------------------------------------------------------------------


class TestBuildContext:
    """构建 AI 上下文字符串。"""

    def test_basic_context(self):
        from src.utils.crypto_fetcher import _build_context

        data = {
            "current_price": 68500.0,
            "price_change_24h_pct": 2.5,
            "price_change_7d_pct": -3.1,
            "price_change_30d_pct": 8.2,
            "high_24h": 69000.0,
            "low_24h": 67000.0,
            "total_volume_24h": 45_000_000_000,
        }
        ctx = _build_context("bitcoin", data, 70000.0, "Will Bitcoin be above $70,000?")
        assert "Bitcoin" in ctx
        assert "$68,500.00" in ctx
        assert "24h change" in ctx
        assert "Target price" in ctx
        assert "below" in ctx  # current 68500 < target 70000

    def test_context_with_above_target(self):
        from src.utils.crypto_fetcher import _build_context

        data = {
            "current_price": 75000.0,
            "price_change_24h_pct": 1.0,
        }
        ctx = _build_context("bitcoin", data, 70000.0, "Will Bitcoin be above $70,000?")
        assert "ABOVE target" in ctx

    def test_context_without_target(self):
        from src.utils.crypto_fetcher import _build_context

        data = {
            "current_price": 68500.0,
            "price_change_24h_pct": 2.5,
        }
        ctx = _build_context("bitcoin", data, None, "Will Bitcoin crash?")
        assert "$68,500.00" in ctx
        assert "Target" not in ctx


# ------------------------------------------------------------------
# fetch_crypto_context 集成测试（mock API）
# ------------------------------------------------------------------


class TestFetchCryptoContext:
    """完整流程测试（mock CoinGecko API）。"""

    @patch("src.utils.crypto_fetcher._get_coin_data")
    def test_full_flow(self, mock_get):
        from src.utils.crypto_fetcher import fetch_crypto_context

        mock_get.return_value = {
            "current_price": 68500.0,
            "price_change_24h_pct": 2.5,
            "price_change_7d_pct": -3.1,
            "price_change_30d_pct": 8.2,
            "high_24h": 69000.0,
            "low_24h": 67000.0,
            "total_volume_24h": 45_000_000_000,
            "sparkline_7d_high": 71000.0,
            "sparkline_7d_low": 65000.0,
            "volatility_7d_pct": 8.8,
        }
        result = fetch_crypto_context(
            "Will the price of Bitcoin be above $70,000 on March 16?"
        )
        assert "Bitcoin" in result
        assert "$68,500.00" in result
        assert "70,000" in result  # target price
        mock_get.assert_called_once_with("bitcoin")

    @patch("src.utils.crypto_fetcher._get_coin_data")
    def test_no_coin_found(self, mock_get):
        from src.utils.crypto_fetcher import fetch_crypto_context

        result = fetch_crypto_context("Will gold reach $3000?")
        assert result == ""
        mock_get.assert_not_called()

    @patch("src.utils.crypto_fetcher._get_coin_data")
    def test_api_failure_returns_empty(self, mock_get):
        from src.utils.crypto_fetcher import fetch_crypto_context

        mock_get.return_value = None
        result = fetch_crypto_context("Will Bitcoin reach $100,000?")
        assert result == ""


# ------------------------------------------------------------------
# _get_coin_data 缓存测试
# ------------------------------------------------------------------


class TestCoinDataCache:
    """验证缓存机制。"""

    @patch("src.utils.crypto_fetcher.requests.get")
    def test_cache_hit(self, mock_requests_get):
        # 预填缓存
        import time

        from src.utils.crypto_fetcher import _cache, _get_coin_data

        _cache["bitcoin"] = (
            time.time(),
            {"current_price": 68500.0},
        )

        result = _get_coin_data("bitcoin")
        assert result["current_price"] == 68500.0
        mock_requests_get.assert_not_called()

        # 清理缓存
        _cache.pop("bitcoin", None)

    @patch("src.utils.crypto_fetcher.requests.get")
    def test_cache_miss_after_ttl(self, mock_requests_get):
        import time

        from src.utils.crypto_fetcher import _CACHE_TTL, _cache, _get_coin_data

        # 过期缓存
        _cache["ethereum"] = (
            time.time() - _CACHE_TTL - 10,
            {"current_price": 2000.0},
        )

        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "market_data": {
                "current_price": {"usd": 2200.0},
                "price_change_percentage_24h": 1.5,
                "price_change_percentage_7d": -2.0,
                "price_change_percentage_30d": 5.0,
                "high_24h": {"usd": 2250.0},
                "low_24h": {"usd": 2150.0},
                "ath": {"usd": 4800.0},
                "ath_change_percentage": {"usd": -54.0},
                "market_cap": {"usd": 260_000_000_000},
                "total_volume": {"usd": 15_000_000_000},
                "sparkline_7d": {"price": [2100, 2150, 2200, 2180, 2220]},
            }
        }
        mock_resp.raise_for_status = MagicMock()
        mock_requests_get.return_value = mock_resp

        result = _get_coin_data("ethereum")
        assert result is not None
        assert result["current_price"] == 2200.0
        mock_requests_get.assert_called_once()

        # 清理缓存
        _cache.pop("ethereum", None)
