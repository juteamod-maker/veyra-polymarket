"""
Tests for orderbook_history module.

官方 Polymarket CLOB API 端点:
  GET /book?token_id=<TOKEN_ID>           — 当前订单簿
  GET /prices-history?market=<TOKEN_ID>   — 历史价格
  GET /spread?token_id=<TOKEN_ID>         — 当前价差
"""

from unittest.mock import MagicMock, patch

import pytest


class TestFetchOrderbook:
    """Tests for fetch_orderbook() — GET /book?token_id="""

    def setup_method(self):
        """Clear module-level cache before each test to prevent cache contamination."""
        import src.utils.orderbook_history as mod

        mod._CACHE.clear()
        mod._TOMBSTONE_CACHE.clear()

    @patch("src.utils.orderbook_history.requests.get")
    def test_success_returns_book_dict(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "market": "0xabc",
            "asset_id": "123456789",
            "timestamp": "1700000000000",
            "bids": [{"price": "0.55", "size": "100"}],
            "asks": [{"price": "0.57", "size": "80"}],
            "min_order_size": "5",
            "tick_size": "0.01",
            "neg_risk": False,
            "last_trade_price": "0.56",
        }
        mock_get.return_value = mock_resp

        from src.utils.orderbook_history import fetch_orderbook

        result = fetch_orderbook("111111111111")
        assert result is not None
        assert "bids" in result
        assert "asks" in result
        assert result["last_trade_price"] == "0.56"
        # Must call /book endpoint, NOT orderbook-history
        args, kwargs = mock_get.call_args
        assert args[0].endswith("/book")
        assert kwargs["params"]["token_id"] == "111111111111"

    @patch("src.utils.orderbook_history.requests.get")
    def test_network_error_returns_none(self, mock_get):
        import requests as req

        mock_get.side_effect = req.ConnectionError("timeout")

        from src.utils.orderbook_history import fetch_orderbook

        result = fetch_orderbook("222222222222")
        assert result is None

    @patch("src.utils.orderbook_history.requests.get")
    def test_http_error_returns_none(self, mock_get):
        import requests as req

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = req.HTTPError("404")
        mock_get.return_value = mock_resp

        from src.utils.orderbook_history import fetch_orderbook

        result = fetch_orderbook("333333333333")
        assert result is None

    def test_invalid_token_id_returns_none(self):
        from src.utils.orderbook_history import fetch_orderbook

        assert fetch_orderbook("") is None
        assert fetch_orderbook("  ") is None
        assert fetch_orderbook("not_a_number") is None

    @patch("src.utils.orderbook_history.requests.get")
    def test_unexpected_format_returns_none(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"error": "No orderbook exists"}
        mock_get.return_value = mock_resp

        from src.utils.orderbook_history import fetch_orderbook

        result = fetch_orderbook("444444444444")
        assert result is None

    @patch("src.utils.orderbook_history.requests.get")
    def test_caches_result(self, mock_get):
        """同一 token_id 的第二次调用不应再发 HTTP 请求（缓存命中）"""
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "market": "0x1",
            "asset_id": "999888777",
            "bids": [{"price": "0.4", "size": "10"}],
            "asks": [{"price": "0.6", "size": "10"}],
        }
        mock_get.return_value = mock_resp

        from src.utils.orderbook_history import fetch_orderbook

        # setup_method already cleared the cache; call twice to verify caching
        fetch_orderbook("999888777")
        fetch_orderbook("999888777")  # second call — should hit cache
        assert mock_get.call_count == 1

    @patch("src.utils.orderbook_history.requests.get")
    def test_404_downgraded_to_debug_not_warning(self, mock_get):
        """市场已关闭时的 HTTP 404 应降级为 DEBUG，不触发 WARNING。"""
        import requests as req
        import src.utils.orderbook_history as mod

        mod._TOMBSTONE_CACHE.clear()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        http_err = req.HTTPError("404 Not Found")
        http_err.response = mock_resp
        mock_resp.raise_for_status.side_effect = http_err
        mock_get.return_value = mock_resp

        from src.utils.orderbook_history import fetch_orderbook

        with patch.object(mod.logger, "warning") as mock_warn, patch.object(
            mod.logger, "debug"
        ) as mock_debug:
            result = fetch_orderbook("43579331175090501")
            assert result is None
            mock_warn.assert_not_called()
            # DEBUG 应该被调用（记录市场已关闭）
            assert mock_debug.call_count >= 1

    @patch("src.utils.orderbook_history.requests.get")
    def test_404_adds_to_tombstone_cache(self, mock_get):
        """404 响应应将 token_id 加入 tombstone 缓存。"""
        import requests as req
        import src.utils.orderbook_history as mod

        mod._TOMBSTONE_CACHE.clear()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        http_err = req.HTTPError("404")
        http_err.response = mock_resp
        mock_resp.raise_for_status.side_effect = http_err
        mock_get.return_value = mock_resp

        from src.utils.orderbook_history import fetch_orderbook

        token = "43579331175090502"
        fetch_orderbook(token)
        assert token in mod._TOMBSTONE_CACHE

    @patch("src.utils.orderbook_history.requests.get")
    def test_tombstone_prevents_repeated_api_calls(self, mock_get):
        """tombstone 缓存内连续调用不应再发 HTTP 请求。"""
        import requests as req
        import src.utils.orderbook_history as mod

        mod._TOMBSTONE_CACHE.clear()
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        http_err = req.HTTPError("404")
        http_err.response = mock_resp
        mock_resp.raise_for_status.side_effect = http_err
        mock_get.return_value = mock_resp

        from src.utils.orderbook_history import fetch_orderbook

        token = "43579331175090503"
        fetch_orderbook(token)  # 第一次：发 HTTP → 404 → 写 tombstone
        fetch_orderbook(token)  # 第二次：tombstone 命中 → 不发 HTTP
        fetch_orderbook(token)  # 第三次：tombstone 命中 → 不发 HTTP
        assert mock_get.call_count == 1  # 只有第一次发请求


class TestFetchPricesHistory:
    """Tests for fetch_prices_history() — GET /prices-history"""

    def setup_method(self):
        """Clear module-level cache before each test to prevent cache contamination."""
        import src.utils.orderbook_history as mod

        mod._CACHE.clear()
        mod._TOMBSTONE_CACHE.clear()

    @patch("src.utils.orderbook_history.requests.get")
    def test_returns_history_list(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            "history": [
                {"t": 1700000000, "p": 0.5},
                {"t": 1700003600, "p": 0.55},
                {"t": 1700007200, "p": 0.52},
            ]
        }
        mock_get.return_value = mock_resp

        from src.utils.orderbook_history import fetch_prices_history

        result = fetch_prices_history("601000000001", interval="1d", fidelity=60)
        assert isinstance(result, list)
        assert len(result) == 3
        assert result[0]["t"] == 1700000000
        assert result[0]["p"] == 0.5
        # Must call /prices-history with 'market' param
        args, kwargs = mock_get.call_args
        assert "/prices-history" in args[0]
        assert kwargs["params"]["market"] == "601000000001"
        assert kwargs["params"]["interval"] == "1d"
        assert kwargs["params"]["fidelity"] == 60

    @patch("src.utils.orderbook_history.requests.get")
    def test_network_error_returns_empty_list(self, mock_get):
        import requests as req

        mock_get.side_effect = req.ConnectionError("timeout")

        from src.utils.orderbook_history import fetch_prices_history

        result = fetch_prices_history("602000000001")
        assert result == []

    def test_empty_token_id_returns_empty_list(self):
        from src.utils.orderbook_history import fetch_prices_history

        assert fetch_prices_history("") == []

    @patch("src.utils.orderbook_history.requests.get")
    def test_invalid_interval_falls_back_to_1d(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"history": []}
        mock_get.return_value = mock_resp

        from src.utils.orderbook_history import fetch_prices_history

        fetch_prices_history("603000000001", interval="INVALID_X")
        _, kwargs = mock_get.call_args
        assert kwargs["params"]["interval"] == "1d"

    @patch("src.utils.orderbook_history.requests.get")
    def test_all_valid_intervals_accepted(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"history": []}
        mock_get.return_value = mock_resp

        from src.utils.orderbook_history import fetch_prices_history

        for iv in ("1h", "6h", "1d", "1w", "max"):
            mock_get.reset_mock()
            fetch_prices_history("604000000001", interval=iv)
            _, kwargs = mock_get.call_args
            assert kwargs["params"]["interval"] == iv


class TestFetchSpread:
    """Tests for fetch_spread() — GET /spread?token_id="""

    @patch("src.utils.orderbook_history.requests.get")
    def test_returns_float(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"spread": "0.05"}
        mock_get.return_value = mock_resp

        from src.utils.orderbook_history import fetch_spread

        result = fetch_spread("123456789")
        assert result == 0.05
        assert isinstance(result, float)
        args, kwargs = mock_get.call_args
        assert "/spread" in args[0]
        assert kwargs["params"]["token_id"] == "123456789"

    @patch("src.utils.orderbook_history.requests.get")
    def test_network_error_returns_none(self, mock_get):
        import requests as req

        mock_get.side_effect = req.ConnectionError("timeout")

        from src.utils.orderbook_history import fetch_spread

        result = fetch_spread("123456789")
        assert result is None

    def test_empty_token_returns_none(self):
        from src.utils.orderbook_history import fetch_spread

        assert fetch_spread("") is None

    @patch("src.utils.orderbook_history.requests.get")
    def test_missing_spread_key_returns_none(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {}
        mock_get.return_value = mock_resp

        from src.utils.orderbook_history import fetch_spread

        assert fetch_spread("123456789") is None


class TestAnalyzeLiquidity:
    """Tests for analyze_liquidity() — works on single /book snapshot."""

    def test_empty_book_returns_defaults(self):
        from src.utils.orderbook_history import analyze_liquidity

        result = analyze_liquidity({})
        assert result["count"] == 0
        assert result["spread"] is None
        assert result["best_bid"] is None
        assert result["best_ask"] is None
        assert result["bid_depth"] == 0.0
        assert result["ask_depth"] == 0.0
        assert result["effective_bid_depth"] == 0.0
        assert result["effective_ask_depth"] == 0.0
        assert result["last_trade_price"] is None

    def test_none_input_returns_defaults(self):
        from src.utils.orderbook_history import analyze_liquidity

        result = analyze_liquidity(None)  # type: ignore[arg-type]
        assert result["count"] == 0
        assert result["spread"] is None

    def test_full_book_computes_correctly(self):
        from src.utils.orderbook_history import analyze_liquidity

        book = {
            "bids": [
                {"price": "0.55", "size": "100"},
                {"price": "0.54", "size": "200"},
            ],
            "asks": [
                {"price": "0.57", "size": "80"},
                {"price": "0.58", "size": "150"},
            ],
            "last_trade_price": "0.56",
        }
        result = analyze_liquidity(book)
        assert result["count"] == 2
        assert result["best_bid"] == 0.55
        assert result["best_ask"] == 0.57
        assert result["spread"] == round(0.57 - 0.55, 4)
        assert result["bid_depth"] > 0
        assert result["ask_depth"] > 0
        assert result["last_trade_price"] == 0.56

    def test_ascending_bids_descending_asks(self):
        """CLOB API 实际返回 bids 升序、asks 降序，verify max/min 逻辑正确。"""
        from src.utils.orderbook_history import analyze_liquidity

        book = {
            "bids": [
                {"price": "0.01", "size": "50000"},
                {"price": "0.10", "size": "500"},
                {"price": "0.55", "size": "100"},
                {"price": "0.65", "size": "1200"},
            ],
            "asks": [
                {"price": "0.99", "size": "50000"},
                {"price": "0.90", "size": "500"},
                {"price": "0.70", "size": "80"},
                {"price": "0.68", "size": "1500"},
            ],
            "last_trade_price": "0.66",
        }
        result = analyze_liquidity(book)
        assert (
            result["best_bid"] == 0.65
        ), f"best_bid should be max of bids, got {result['best_bid']}"
        assert (
            result["best_ask"] == 0.68
        ), f"best_ask should be min of asks, got {result['best_ask']}"
        assert result["spread"] == round(0.68 - 0.65, 4)

    def test_bids_only_spread_is_none(self):
        from src.utils.orderbook_history import analyze_liquidity

        book = {
            "bids": [{"price": "0.48", "size": "10"}],
            "asks": [],
        }
        result = analyze_liquidity(book)
        assert result["count"] == 1
        assert result["spread"] is None
        assert result["best_bid"] == 0.48
        assert result["best_ask"] is None

    def test_asks_only_spread_is_none(self):
        from src.utils.orderbook_history import analyze_liquidity

        book = {
            "bids": [],
            "asks": [{"price": "0.60", "size": "50"}],
        }
        result = analyze_liquidity(book)
        assert result["count"] == 0
        assert result["spread"] is None
        assert result["best_ask"] == 0.60


class TestAnalyzePriceTrend:
    """Tests for analyze_price_trend() — works on fetch_prices_history() output."""

    def test_empty_history_returns_defaults(self):
        from src.utils.orderbook_history import analyze_price_trend

        result = analyze_price_trend([])
        assert result["count"] == 0
        assert result["trend"] is None

    def test_uptrend_detected(self):
        from src.utils.orderbook_history import analyze_price_trend

        history = [
            {"t": 1000, "p": 0.40},
            {"t": 1060, "p": 0.45},
            {"t": 1120, "p": 0.50},
        ]
        result = analyze_price_trend(history)
        assert result["trend"] == "up"
        assert result["price_current"] == 0.50
        assert result["price_oldest"] == 0.40
        assert result["price_change"] > 0

    def test_downtrend_detected(self):
        from src.utils.orderbook_history import analyze_price_trend

        history = [
            {"t": 1000, "p": 0.60},
            {"t": 1060, "p": 0.55},
            {"t": 1120, "p": 0.50},
        ]
        result = analyze_price_trend(history)
        assert result["trend"] == "down"
        assert result["price_change"] < 0

    def test_flat_trend_detected(self):
        from src.utils.orderbook_history import analyze_price_trend

        history = [{"t": i * 60, "p": 0.50} for i in range(5)]
        result = analyze_price_trend(history)
        assert result["trend"] == "flat"
        assert result["price_change"] == 0.0

    def test_min_max_computed(self):
        from src.utils.orderbook_history import analyze_price_trend

        history = [
            {"t": 1000, "p": 0.30},
            {"t": 1060, "p": 0.70},
            {"t": 1120, "p": 0.50},
        ]
        result = analyze_price_trend(history)
        assert result["price_min"] == 0.30
        assert result["price_max"] == 0.70
