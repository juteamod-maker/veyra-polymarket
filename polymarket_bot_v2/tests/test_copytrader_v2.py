"""T365-Wave2 Copytrader V2 unit tests."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from src.services.copytrader_v2.official_client import PolymarketOfficialClient
from src.services.copytrader_v2.wallet_analyzer import (
    WalletAnalyzer,
    detect_category,
    filter_and_rank,
)


class TestDetectCategory:
    def test_weather_detection(self) -> None:
        assert detect_category("highest-temperature-tokyo-2026", "Highest Temperature in Tokyo") == "weather"

    def test_sports_detection(self) -> None:
        assert detect_category("nba-finals-2026", "NBA Finals 2026 Champion") == "sports"

    def test_crypto_detection(self) -> None:
        assert detect_category("btc-100k", "Will BTC hit 100k?") == "crypto"

    def test_other_for_unknown(self) -> None:
        assert detect_category("foo-bar", "Foo Bar") == "other"


class TestFilterAndRank:
    def test_filter_min_score(self) -> None:
        rows = [
            {"address": "0xa", "score": 80, "pnl_total_usd": 1000, "trade_count_30d": 30, "win_rate": 0.6, "is_likely_bot": False, "primary_category": "weather", "category_distribution": {"weather": 10}},
            {"address": "0xb", "score": 40, "pnl_total_usd": 500, "trade_count_30d": 10, "win_rate": 0.55, "is_likely_bot": False, "primary_category": "sports", "category_distribution": {"sports": 5}},
        ]
        out = filter_and_rank(rows, min_score=70)
        assert len(out) == 1
        assert out[0]["address"] == "0xa"

    def test_exclude_bots(self) -> None:
        rows = [
            {"address": "0xa", "score": 90, "pnl_total_usd": 10000, "trade_count_30d": 50, "win_rate": 0.7, "is_likely_bot": True, "primary_category": "crypto", "category_distribution": {}},
            {"address": "0xb", "score": 70, "pnl_total_usd": 8000, "trade_count_30d": 40, "win_rate": 0.65, "is_likely_bot": False, "primary_category": "crypto", "category_distribution": {}},
        ]
        out = filter_and_rank(rows, exclude_bots=True)
        assert len(out) == 1 and out[0]["address"] == "0xb"

    def test_category_filter(self) -> None:
        rows = [
            {"address": "0xa", "score": 80, "pnl_total_usd": 1000, "trade_count_30d": 30, "win_rate": 0.6, "is_likely_bot": False, "primary_category": "weather", "category_distribution": {"weather": 10}},
            {"address": "0xb", "score": 80, "pnl_total_usd": 1000, "trade_count_30d": 30, "win_rate": 0.6, "is_likely_bot": False, "primary_category": "sports", "category_distribution": {"sports": 10}},
        ]
        out = filter_and_rank(rows, category="weather")
        assert len(out) == 1 and out[0]["address"] == "0xa"


class TestOfficialClient:
    def test_no_third_party_polyhub(self) -> None:
        import inspect
        src = inspect.getsource(PolymarketOfficialClient)
        assert "polyhub" not in src.lower()
        assert "hubble-rpc" not in src.lower()
        assert "skill-test" not in src.lower()

    def test_data_api_endpoint_official(self) -> None:
        from src.services.copytrader_v2 import official_client as oc
        assert oc.DATA_API == "https://data-api.polymarket.com"
        assert oc.GAMMA_API == "https://gamma-api.polymarket.com"
        assert oc.CLOB_API == "https://clob.polymarket.com"

    def test_get_positions_returns_list(self) -> None:
        client = PolymarketOfficialClient()
        with patch.object(client, "_get", return_value=[{"slug": "x", "title": "y"}]):
            out = client.get_positions("0xabc")
        assert isinstance(out, list) and out[0]["slug"] == "x"

    def test_get_positions_empty_address(self) -> None:
        client = PolymarketOfficialClient()
        assert client.get_positions("") == []


class TestWalletAnalyzer:
    def test_empty_metrics_for_invalid_address(self) -> None:
        analyzer = WalletAnalyzer()
        m = analyzer.compute_metrics("not-0x")
        assert m["data_complete"] is False
        assert m["score"] == 0.0

    def test_compute_metrics_with_mock(self) -> None:
        analyzer = WalletAnalyzer()
        positions = [
            {"slug": "highest-temp-tokyo", "title": "Highest Temp Tokyo", "currentValue": 100, "cashPnl": 20},
        ]
        value = {"value": 5000, "realizedPnl": 1500}
        activity = [
            {"type": "TRADE", "timestamp": 1000000, "pnl": 100, "slug": "highest-temp-tokyo", "title": "Highest Temp"},
            {"type": "TRADE", "timestamp": 1000100, "pnl": -50, "slug": "highest-temp-tokyo", "title": "Highest Temp"},
        ]
        with patch.object(analyzer._client, "get_positions", return_value=positions), \
             patch.object(analyzer._client, "get_value", return_value=value), \
             patch.object(analyzer._client, "get_activity", return_value=activity):
            m = analyzer.compute_metrics("0x" + "a" * 40)
        assert m["data_complete"] is True
        assert m["pnl_realized_usd"] == 1500.0
        assert m["pnl_unrealized_usd"] == 20.0
        assert m["primary_category"] in ("weather", "other")
        assert m["trade_count_total"] == 2
