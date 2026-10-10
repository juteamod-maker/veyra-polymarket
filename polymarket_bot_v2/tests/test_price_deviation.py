"""
test_price_deviation.py — PriceDeviation 智能参考价刷新测试（Phase 53）

验证 outcome_tracker._validate_orderbook_price 的改进：
  1. 阈值从 config 读取（不再硬编码 0.25）
  2. 参考价过期（max_age）自动信任 orderbook
  3. 偏离 > auto_update_threshold 时自动刷新缓存
  4. 正常偏离不触发替换
"""

import time
from unittest.mock import patch

import pytest
from src.simulation import outcome_tracker as ot_module
from src.simulation.outcome_tracker import OutcomeTracker

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_caches():
    """每个测试前清理模块级缓存。"""
    ot_module._price_deviation_seen.clear()
    ot_module._ref_price_cache.clear()
    yield
    ot_module._price_deviation_seen.clear()
    ot_module._ref_price_cache.clear()


_DEFAULT_SIG_CFG = {
    "price_deviation_soft_threshold": 0.8,
    "price_deviation_ref_auto_update_threshold": 0.4,
    "price_deviation_ref_max_age_seconds": 60,
}


def _mock_get(path):
    if path == "signal":
        return dict(_DEFAULT_SIG_CFG)
    return None


# ===========================================================================
# 1. 基础阈值测试（config 驱动）
# ===========================================================================


class TestConfigThreshold:
    """阈值从 config.signal 读取，不再硬编码 0.25。"""

    @patch("src.simulation.outcome_tracker.get", side_effect=_mock_get)
    def test_within_threshold_returns_raw(self, _mock):
        """偏离 < 80% → 返回原始价格。"""
        # ref=0.10, raw=0.17 → deviation=70% < 80%
        result = OutcomeTracker._validate_orderbook_price(
            0.17, 0.10, "bestAsk", "mkt-1"
        )
        assert result == 0.17

    @patch("src.simulation.outcome_tracker.get", side_effect=_mock_get)
    def test_exceeds_threshold_returns_ref(self, _mock):
        """偏离 > 80% → 返回参考价。"""
        # 先给 cache 一个新鲜的条目，防止 auto_update 触发
        ot_module._ref_price_cache["mkt-2"] = (0.10, time.monotonic())
        # ref=0.10, raw=0.20 → deviation=100% > 80%
        result = OutcomeTracker._validate_orderbook_price(
            0.20, 0.10, "bestAsk", "mkt-2"
        )
        assert result == 0.10

    @patch("src.simulation.outcome_tracker.get", side_effect=_mock_get)
    def test_none_ref_returns_raw(self, _mock):
        """ref_price=None → 直接返回原始价格。"""
        result = OutcomeTracker._validate_orderbook_price(
            0.55, None, "bestBid", "mkt-3"
        )
        assert result == 0.55

    @patch("src.simulation.outcome_tracker.get", side_effect=_mock_get)
    def test_zero_raw_returns_raw(self, _mock):
        """raw_price=0 → 直接返回（不做校验）。"""
        result = OutcomeTracker._validate_orderbook_price(
            0.0, 0.50, "bestBid", "mkt-4"
        )
        assert result == 0.0


# ===========================================================================
# 2. 参考价自动刷新测试
# ===========================================================================


class TestAutoRefresh:
    """偏离 > auto_update_threshold 且参考价过旧 → 自动刷新。"""

    @patch("src.simulation.outcome_tracker.get", side_effect=_mock_get)
    def test_stale_ref_trusts_orderbook(self, _mock):
        """参考价过期 (>60s) + 偏离 > 40% → 信任 orderbook 价格。"""
        # 在缓存中放入一个 120 秒前的条目
        stale_ts = time.monotonic() - 120
        ot_module._ref_price_cache["mkt-stale"] = (0.02, stale_ts)

        # ref=0.02, raw=0.055 → deviation=175% > 40%
        result = OutcomeTracker._validate_orderbook_price(
            0.055, 0.02, "bestAsk", "mkt-stale"
        )
        assert result == 0.055

        # 缓存应已更新为新的 orderbook 价格
        cached_ref, cached_ts = ot_module._ref_price_cache["mkt-stale"]
        assert cached_ref == 0.055
        assert cached_ts > stale_ts

    @patch("src.simulation.outcome_tracker.get", side_effect=_mock_get)
    def test_fresh_ref_still_replaces(self, _mock):
        """参考价新鲜 (<60s) + 偏离 > 80% → 仍用参考价替代。"""
        fresh_ts = time.monotonic()
        ot_module._ref_price_cache["mkt-fresh"] = (0.10, fresh_ts)

        # ref=0.10, raw=0.20 → deviation=100% > 80%（但 ref 新鲜）
        result = OutcomeTracker._validate_orderbook_price(
            0.20, 0.10, "bestAsk", "mkt-fresh"
        )
        assert result == 0.10

    @patch("src.simulation.outcome_tracker.get", side_effect=_mock_get)
    def test_first_seen_market_records_ref(self, _mock):
        """首次遇到市场 → 记录参考价到缓存。"""
        assert "mkt-new" not in ot_module._ref_price_cache

        # ref=0.10, raw=0.15 → deviation=50% > 40% → 首次记录
        # 但因为没有缓存条目（首次）→ 不会触发 auto_update → 继续正常流程
        result = OutcomeTracker._validate_orderbook_price(
            0.15, 0.10, "bestAsk", "mkt-new"
        )
        # deviation=50% < 80% → 返回 raw
        assert result == 0.15
        # 首次市场应已记录参考价
        assert "mkt-new" in ot_module._ref_price_cache


# ===========================================================================
# 3. Max Age 过期测试
# ===========================================================================


class TestMaxAge:
    """缓存过期时直接信任 orderbook。"""

    @patch("src.simulation.outcome_tracker.get", side_effect=_mock_get)
    def test_expired_cache_trusts_orderbook(self, _mock):
        """缓存过期 (>60s) → 不论偏离大小都信任 orderbook。"""
        expired_ts = time.monotonic() - 90
        ot_module._ref_price_cache["mkt-exp"] = (0.50, expired_ts)

        # ref=0.50, raw=0.55 → deviation=10%（很小）
        # 但缓存过期 → 直接信任 orderbook
        result = OutcomeTracker._validate_orderbook_price(
            0.55, 0.50, "bestBid", "mkt-exp"
        )
        assert result == 0.55

        # 缓存应已刷新
        cached_ref, _ = ot_module._ref_price_cache["mkt-exp"]
        assert cached_ref == 0.55

    @patch("src.simulation.outcome_tracker.get", side_effect=_mock_get)
    def test_not_expired_cache_normal_logic(self, _mock):
        """缓存未过期 → 正常阈值判断。"""
        fresh_ts = time.monotonic() - 10  # 10s ago
        ot_module._ref_price_cache["mkt-ok"] = (0.30, fresh_ts)

        # ref=0.30, raw=0.32 → deviation=6.7% < 80% → 返回 raw
        result = OutcomeTracker._validate_orderbook_price(
            0.32, 0.30, "bestAsk", "mkt-ok"
        )
        assert result == 0.32


# ===========================================================================
# 4. 日志去重测试
# ===========================================================================


class TestLogDedup:
    """同一 market:label 60 秒内只警告一次。"""

    @patch("src.simulation.outcome_tracker.get", side_effect=_mock_get)
    def test_dedup_suppresses_repeat_logs(self, _mock):
        """连续调用 → 第二次不重复打日志，但仍返回参考价。"""
        ot_module._ref_price_cache["mkt-dup"] = (0.10, time.monotonic())

        r1 = OutcomeTracker._validate_orderbook_price(
            0.20, 0.10, "bestAsk", "mkt-dup"
        )
        r2 = OutcomeTracker._validate_orderbook_price(
            0.20, 0.10, "bestAsk", "mkt-dup"
        )
        assert r1 == 0.10
        assert r2 == 0.10
        # 去重记录应存在
        assert "mkt-dup:bestAsk" in ot_module._price_deviation_seen


# ===========================================================================
# 5. 用户场景复现（log 中看到的 +175% 偏离）
# ===========================================================================


class TestRealWorldScenario:
    """复现用户报告的场景：bestAsk=0.055 vs ref=0.02 (deviation=+175%)。"""

    @patch("src.simulation.outcome_tracker.get", side_effect=_mock_get)
    def test_stale_weather_market_auto_refreshes(self, _mock):
        """
        天气市场：ref=0.02 已过期 → bestAsk=0.055 是真实价格 → 自动刷新。
        修复前：deviation=175% > 25% → 错误地使用 ref=0.02 替代
        修复后：ref 过期 → 信任 orderbook → 返回 0.055
        """
        # 模拟 120 秒前的旧参考价
        stale_ts = time.monotonic() - 120
        ot_module._ref_price_cache["1757143"] = (0.02, stale_ts)

        result = OutcomeTracker._validate_orderbook_price(
            0.055, 0.02, "bestAsk", "1757143"
        )
        assert result == 0.055  # 应该信任 orderbook

    @patch("src.simulation.outcome_tracker.get", side_effect=_mock_get)
    def test_fresh_weather_market_normal_flow(self, _mock):
        """参考价新鲜 → deviation=175% > 80% → 仍替换（保护）。"""
        fresh_ts = time.monotonic()
        ot_module._ref_price_cache["1757143"] = (0.02, fresh_ts)

        result = OutcomeTracker._validate_orderbook_price(
            0.055, 0.02, "bestAsk", "1757143"
        )
        # 新鲜参考价 + deviation=175% > 80% → 使用参考价
        assert result == 0.02


# ===========================================================================
# 6. _get_reference_price 不变性测试
# ===========================================================================


class TestGetReferencePrice:
    """确保静态方法 _get_reference_price 行为不变。"""

    def test_lastTradePrice_priority(self):
        data = {"lastTradePrice": 0.42, "tokens": [{"outcome": "Yes", "price": 0.50}]}
        assert OutcomeTracker._get_reference_price(data) == 0.42

    def test_tokens_fallback(self):
        data = {"lastTradePrice": None, "tokens": [{"outcome": "Yes", "price": 0.35}]}
        assert OutcomeTracker._get_reference_price(data) == 0.35

    def test_outcome_prices_fallback(self):
        import json
        data = {
            "lastTradePrice": None,
            "tokens": [],
            "outcomePrices": json.dumps([0.60, 0.40]),
        }
        assert OutcomeTracker._get_reference_price(data) == 0.60

    def test_no_data_returns_none(self):
        assert OutcomeTracker._get_reference_price({}) is None
