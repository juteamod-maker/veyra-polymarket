"""T224: Dashboard 实时价格 CLOB REST API 切换测试

验证:
1. _resolve_token_ids 正确解析 Gamma clobTokenIds
2. _fetch_clob_sell_price 调用 CLOB /price?side=SELL
3. _fetch_single_market 优先 CLOB, 失败降级 Gamma
4. _get_current_price_for_bet 优先使用 outcome_prices(CLOB直接价)
5. _REALTIME_CACHE_TTL 为 8 秒
"""

import json
from unittest.mock import MagicMock, patch

import pytest

# ─── fixtures ───


@pytest.fixture(autouse=True)
def _clear_caches():
    """每个测试前清空缓存。"""
    from scripts.dashboard_routes import data

    data._per_market_price_cache.clear()
    data._token_id_cache.clear()
    yield
    data._per_market_price_cache.clear()
    data._token_id_cache.clear()


# ─── 1. TTL 验证 ───


class TestCacheTTL:
    def test_realtime_cache_ttl_is_8_seconds(self):
        from scripts.dashboard_routes.data import _REALTIME_CACHE_TTL

        assert _REALTIME_CACHE_TTL == 8, "T224: TTL 应从 60s 降至 8s"


# ─── 2. Token ID 解析 ───


class TestResolveTokenIds:
    @patch("scripts.dashboard_routes.data.requests.get")
    def test_numeric_market_id(self, mock_get):
        from scripts.dashboard_routes.data import _resolve_token_ids

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "clobTokenIds": json.dumps(["yes_tok_111", "no_tok_222"]),
        }
        mock_get.return_value = mock_resp

        result = _resolve_token_ids("12345")
        assert result == {"YES": "yes_tok_111", "NO": "no_tok_222"}
        mock_get.assert_called_once()
        assert "/markets/12345" in mock_get.call_args[0][0]

    @patch("scripts.dashboard_routes.data.requests.get")
    def test_0x_market_id_with_slug(self, mock_get):
        from scripts.dashboard_routes.data import _resolve_token_ids

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [{"clobTokenIds": ["y_tok", "n_tok"]}]
        mock_get.return_value = mock_resp

        result = _resolve_token_ids("0xabc123", slug="my-market-slug")
        assert result == {"YES": "y_tok", "NO": "n_tok"}
        assert mock_get.call_args[1]["params"] == {"slug": "my-market-slug"}

    @patch("scripts.dashboard_routes.data.requests.get")
    def test_cache_hit_no_api_call(self, mock_get):
        from scripts.dashboard_routes.data import _resolve_token_ids, _token_id_cache

        _token_id_cache["cached_mid"] = {"YES": "y", "NO": "n"}
        result = _resolve_token_ids("cached_mid")
        assert result == {"YES": "y", "NO": "n"}
        mock_get.assert_not_called()

    @patch("scripts.dashboard_routes.data.requests.get")
    def test_gamma_failure_returns_none(self, mock_get):
        from scripts.dashboard_routes.data import _resolve_token_ids

        mock_get.side_effect = Exception("timeout")
        assert _resolve_token_ids("any_id") is None

    @patch("scripts.dashboard_routes.data.requests.get")
    def test_insufficient_clob_ids_returns_none(self, mock_get):
        from scripts.dashboard_routes.data import _resolve_token_ids

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"clobTokenIds": '["only_one"]'}
        mock_get.return_value = mock_resp

        assert _resolve_token_ids("mid") is None


# ─── 3. CLOB 卖出价获取 ───


class TestFetchClobSellPrice:
    @patch("scripts.dashboard_routes.data.requests.get")
    def test_successful_price_fetch(self, mock_get):
        from scripts.dashboard_routes.data import _fetch_clob_sell_price

        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {"price": "0.89"}
        mock_get.return_value = mock_resp

        price = _fetch_clob_sell_price("tok_123")
        assert price == 0.89
        mock_get.assert_called_once()
        args, kwargs = mock_get.call_args
        assert "clob.polymarket.com/price" in args[0]
        assert kwargs["params"] == {"token_id": "tok_123", "side": "SELL"}

    @patch("scripts.dashboard_routes.data.requests.get")
    def test_api_failure_returns_none(self, mock_get):
        from scripts.dashboard_routes.data import _fetch_clob_sell_price

        mock_get.side_effect = Exception("connection refused")
        assert _fetch_clob_sell_price("tok_123") is None

    @patch("scripts.dashboard_routes.data.requests.get")
    def test_non_ok_response_returns_none(self, mock_get):
        from scripts.dashboard_routes.data import _fetch_clob_sell_price

        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_get.return_value = mock_resp

        assert _fetch_clob_sell_price("tok_123") is None


# ─── 4. CLOB-first Market Fetch ───


class TestFetchSingleMarket:
    @patch("scripts.dashboard_routes.data._fetch_clob_sell_price")
    @patch("scripts.dashboard_routes.data._resolve_token_ids")
    def test_clob_success_returns_clob_prices(self, mock_resolve, mock_clob):
        from scripts.dashboard_routes.data import _fetch_single_market

        mock_resolve.return_value = {"YES": "y_tok", "NO": "n_tok"}
        mock_clob.side_effect = lambda tok: 0.20 if tok == "y_tok" else 0.89

        mid, pinfo = _fetch_single_market("12345")
        assert mid == "12345"
        assert pinfo["outcome_prices"]["YES"] == 0.20
        assert pinfo["outcome_prices"]["NO"] == 0.89
        assert pinfo["best_bid"] == 0.20

    @patch("scripts.dashboard_routes.data._fetch_single_market_gamma")
    @patch("scripts.dashboard_routes.data._fetch_clob_sell_price")
    @patch("scripts.dashboard_routes.data._resolve_token_ids")
    def test_clob_failure_falls_back_to_gamma(
        self, mock_resolve, mock_clob, mock_gamma
    ):
        from scripts.dashboard_routes.data import _fetch_single_market

        mock_resolve.return_value = {"YES": "y", "NO": "n"}
        mock_clob.return_value = None  # CLOB 全部失败
        mock_gamma.return_value = (
            "12345",
            {"best_bid": 0.11, "best_ask": 0.14, "outcome_prices": {}},
        )

        mid, pinfo = _fetch_single_market("12345")
        assert mid == "12345"
        assert pinfo["best_bid"] == 0.11  # Gamma fallback
        mock_gamma.assert_called_once()

    @patch("scripts.dashboard_routes.data._fetch_single_market_gamma")
    @patch("scripts.dashboard_routes.data._resolve_token_ids")
    def test_no_token_ids_falls_back_to_gamma(self, mock_resolve, mock_gamma):
        from scripts.dashboard_routes.data import _fetch_single_market

        mock_resolve.return_value = None  # token_id 解析失败
        mock_gamma.return_value = (
            "m1",
            {"best_bid": 0.5, "best_ask": 0.6, "outcome_prices": {}},
        )

        mid, pinfo = _fetch_single_market("m1")
        assert pinfo["best_bid"] == 0.5
        mock_gamma.assert_called_once()


# ─── 5. 价格提取优先级 ───


class TestGetCurrentPriceForBet:
    def test_no_position_prefers_clob_outcome_price(self):
        from scripts.dashboard_routes.data import _get_current_price_for_bet

        bet = {"market_id": "m1", "outcome": "NO"}
        # CLOB 直接 NO 卖出价 = 0.89
        prices = {
            "m1": {
                "best_bid": 0.20,
                "best_ask": 0.14,
                "outcome_prices": {"YES": 0.20, "NO": 0.89},
            }
        }
        cp = _get_current_price_for_bet(bet, prices)
        assert cp == 0.89, "T224: NO 应使用 CLOB 直接 NO 卖出价，而非 1-bestAsk"

    def test_yes_position_prefers_clob_outcome_price(self):
        from scripts.dashboard_routes.data import _get_current_price_for_bet

        bet = {"market_id": "m1", "outcome": "YES"}
        prices = {
            "m1": {
                "best_bid": 0.45,
                "best_ask": None,
                "outcome_prices": {"YES": 0.48, "NO": 0.55},
            }
        }
        cp = _get_current_price_for_bet(bet, prices)
        assert cp == 0.48, "T224: YES 应使用 CLOB 直接 YES 卖出价"

    def test_no_fallback_to_1_minus_best_ask(self):
        from scripts.dashboard_routes.data import _get_current_price_for_bet

        bet = {"market_id": "m1", "outcome": "NO"}
        # Gamma 数据（无 CLOB outcome_prices）
        prices = {
            "m1": {
                "best_bid": 0.11,
                "best_ask": 0.14,
                "outcome_prices": {},  # CLOB 失败，Gamma 无 NO 键
            }
        }
        cp = _get_current_price_for_bet(bet, prices)
        assert cp == round(1.0 - 0.14, 4), "NO fallback：1 - bestAsk(YES)"

    def test_yes_fallback_to_best_bid(self):
        from scripts.dashboard_routes.data import _get_current_price_for_bet

        bet = {"market_id": "m1", "outcome": "YES"}
        prices = {
            "m1": {
                "best_bid": 0.45,
                "best_ask": 0.55,
                "outcome_prices": {},  # 无 CLOB YES
            }
        }
        cp = _get_current_price_for_bet(bet, prices)
        assert cp == 0.45, "YES fallback：Gamma bestBid"

    def test_no_data_returns_none(self):
        from scripts.dashboard_routes.data import _get_current_price_for_bet

        bet = {"market_id": "m1", "outcome": "NO"}
        prices = {}
        assert _get_current_price_for_bet(bet, prices) is None

    def test_critical_comparison_gamma_vs_clob(self):
        """模拟 T224 发现的关键场景：Gamma 严重滞后 vs CLOB 实时。"""
        from scripts.dashboard_routes.data import _get_current_price_for_bet

        bet = {"market_id": "ams19", "outcome": "NO"}

        # 旧方案（Gamma）：bestAsk=0.59 → NO sell = 1-0.59 = 0.41 ← 严重低估！
        gamma_prices = {
            "ams19": {
                "best_bid": 0.06,
                "best_ask": 0.59,
                "outcome_prices": {},
            }
        }
        gamma_cp = _get_current_price_for_bet(bet, gamma_prices)
        assert gamma_cp == round(1.0 - 0.59, 4)  # 0.41

        # 新方案（CLOB）：NO 直接卖出价 = 0.89 ← 实时准确！
        clob_prices = {
            "ams19": {
                "best_bid": 0.20,
                "best_ask": None,
                "outcome_prices": {"YES": 0.20, "NO": 0.89},
            }
        }
        clob_cp = _get_current_price_for_bet(bet, clob_prices)
        assert clob_cp == 0.89  # 正确实时价
        assert clob_cp > gamma_cp  # CLOB 价更准确
