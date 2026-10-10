"""T290: StrikeOpt 超时/限制机制测试。

修复项:
 1. per-event 超时: 单个城市 429 不再阻塞整个周期
 2. 总超时: StrikeOpt 阶段有时间预算
 3. max events 限制: 超出限制的事件 fallback 到 _score 排序
 4. 事件按最高 _score 排序: 高价值事件优先处理
"""

import time
from unittest.mock import MagicMock, patch

import pytest
from src.scanner.market_scanner import MarketScanner


def _make_weather_market(mid, question, event_slug, ask=0.30, bid=0.28, score=60.0):
    return {
        "id": mid,
        "question": question,
        "_event_slug": event_slug,
        "_score": score,
        "bestAsk": ask,
        "bestBid": bid,
        "tags": ["temperature"],
    }


@pytest.fixture
def scanner():
    with patch("src.scanner.market_scanner.get") as mock_get:
        mock_get.return_value = {}
        s = MarketScanner()
    return s


class TestStrikeOptMaxEventsLimit:
    """T290: max_weather_strike_opt_events 限制生效。"""

    @patch("src.scanner.market_scanner.fetch_dual_model_probability")
    @patch("src.scanner.market_scanner.prefetch_ensemble_data")
    @patch("src.scanner.market_scanner.get")
    def test_excess_events_get_fallback(self, mock_get, mock_prefetch, mock_dual):
        """超过 max_events 的事件应 fallback 到 _score 排序。"""

        def config_side_effect(key):
            return {
                "scanner.strike_optimizer_total_timeout_sec": 300,
                "scanner.strike_optimizer_event_timeout_sec": 90,
                "scanner.max_weather_strike_opt_events": 2,
                "weather_city_scanner": {"enabled": False},
            }.get(key)

        mock_get.side_effect = config_side_effect
        mock_dual.return_value = {"probability": 0.15, "confidence": 0.80}

        # 3 events, but max=2
        candidates = []
        for i, city in enumerate(["Dallas", "NYC", "Toronto"]):
            slug = f"{city.lower()}-temp"
            for j in range(3):
                candidates.append(
                    _make_weather_market(
                        f"{city}-{j}",
                        f"Will {city} be {70+j}°F on April 1?",
                        slug,
                        score=80 - i * 10,  # Dallas=80, NYC=70, Toronto=60
                    )
                )

        with patch("src.scanner.market_scanner.get") as mock_get2:
            mock_get2.side_effect = config_side_effect
            scanner = MarketScanner.__new__(MarketScanner)
            result = scanner._weather_strike_optimizer(candidates)

        # All 3 events should be present (2 processed + 1 fallback)
        slugs = [m.get("_event_slug") for m in result]
        assert "dallas-temp" in slugs
        assert "nyc-temp" in slugs
        assert "toronto-temp" in slugs

        # Toronto (lowest score, 3rd event) should have fallback marker
        toronto = [m for m in result if m.get("_event_slug") == "toronto-temp"]
        assert len(toronto) == 1
        assert toronto[0].get("_strike_source") == "timeout_fallback"

    @patch("src.scanner.market_scanner.fetch_dual_model_probability")
    @patch("src.scanner.market_scanner.prefetch_ensemble_data")
    @patch("src.scanner.market_scanner.get")
    def test_high_score_events_processed_first(
        self, mock_get, mock_prefetch, mock_dual
    ):
        """高 _score 事件应优先处理，低分事件先被 fallback。"""

        def config_side_effect(key):
            return {
                "scanner.strike_optimizer_total_timeout_sec": 300,
                "scanner.strike_optimizer_event_timeout_sec": 90,
                "scanner.max_weather_strike_opt_events": 1,
                "weather_city_scanner": {"enabled": False},
            }.get(key)

        mock_get.side_effect = config_side_effect
        mock_dual.return_value = {"probability": 0.10, "confidence": 0.85}

        # 2 events: low_score=40, high_score=90
        candidates = [
            _make_weather_market("low-0", "Will Low be 70°F?", "low-event", score=40),
            _make_weather_market("low-1", "Will Low be 72°F?", "low-event", score=38),
            _make_weather_market(
                "high-0", "Will High be 80°F?", "high-event", score=90
            ),
            _make_weather_market(
                "high-1", "Will High be 82°F?", "high-event", score=88
            ),
        ]

        with patch("src.scanner.market_scanner.get") as mock_get2:
            mock_get2.side_effect = config_side_effect
            scanner = MarketScanner.__new__(MarketScanner)
            result = scanner._weather_strike_optimizer(candidates)

        # High-score event should be processed (not fallback)
        high = [m for m in result if m.get("_event_slug") == "high-event"]
        assert len(high) == 1
        assert high[0].get("_strike_source") != "timeout_fallback"

        # Low-score event should be fallback (exceeded max_events=1)
        low = [m for m in result if m.get("_event_slug") == "low-event"]
        assert len(low) == 1
        assert low[0].get("_strike_source") == "timeout_fallback"


class TestStrikeOptEventTimeout:
    """T290: per-event 超时让一个 429 城市不阻塞后续。"""

    @patch("src.scanner.market_scanner.fetch_dual_model_probability")
    @patch("src.scanner.market_scanner.prefetch_ensemble_data")
    @patch("src.scanner.market_scanner.get")
    def test_event_timeout_uses_partial_results(
        self, mock_get, mock_prefetch, mock_dual
    ):
        """事件超时后如果已有部分结果，应使用已计算的 best EV。"""

        def config_side_effect(key):
            return {
                "scanner.strike_optimizer_total_timeout_sec": 300,
                "scanner.strike_optimizer_event_timeout_sec": 0.001,  # 极短超时
                "scanner.max_weather_strike_opt_events": 10,
                "weather_city_scanner": {"enabled": False},
            }.get(key)

        mock_get.side_effect = config_side_effect

        call_count = 0

        def slow_dual(market):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # 第一个 strike 快速返回
                return {"probability": 0.12, "confidence": 0.85}
            # 后续慢(模拟 429 降级)
            time.sleep(0.01)
            return {"probability": 0.08, "confidence": 0.85}

        mock_dual.side_effect = slow_dual

        group = [
            _make_weather_market(
                f"s{i}", f"Will City be {70+i}°F?", "city-slug", score=60
            )
            for i in range(5)
        ]

        with patch("src.scanner.market_scanner.get") as mock_get2:
            mock_get2.side_effect = config_side_effect
            scanner = MarketScanner.__new__(MarketScanner)
            result = scanner._weather_strike_optimizer(group)

        # Should have one result for this event (partial or full)
        city_results = [m for m in result if m.get("_event_slug") == "city-slug"]
        assert len(city_results) == 1

    @patch("src.scanner.market_scanner.fetch_dual_model_probability")
    @patch("src.scanner.market_scanner.prefetch_ensemble_data")
    @patch("src.scanner.market_scanner.get")
    def test_event_timeout_fallback_to_score(self, mock_get, mock_prefetch, mock_dual):
        """事件超时且无双模型结果时，fallback 到最高 _score。"""

        def config_side_effect(key):
            return {
                "scanner.strike_optimizer_total_timeout_sec": 300,
                "scanner.strike_optimizer_event_timeout_sec": 0.001,
                "scanner.max_weather_strike_opt_events": 10,
                "weather_city_scanner": {"enabled": False},
            }.get(key)

        mock_get.side_effect = config_side_effect

        def always_slow(market):
            time.sleep(0.01)
            return None

        mock_dual.side_effect = always_slow

        group = [
            _make_weather_market("s0", "Will City be 70°F?", "city-slug", score=45),
            _make_weather_market("s1", "Will City be 72°F?", "city-slug", score=80),
            _make_weather_market("s2", "Will City be 74°F?", "city-slug", score=60),
        ]

        with patch("src.scanner.market_scanner.get") as mock_get2:
            mock_get2.side_effect = config_side_effect
            scanner = MarketScanner.__new__(MarketScanner)
            result = scanner._weather_strike_optimizer(group)

        city_results = [m for m in result if m.get("_event_slug") == "city-slug"]
        assert len(city_results) == 1
        # 无双模型结果 → fallback → 最高 _score 的 s1 被选中
        assert city_results[0]["id"] == "s1"


class TestStrikeOptTotalTimeout:
    """T290: StrikeOpt 阶段总超时。"""

    @patch("src.scanner.market_scanner.fetch_dual_model_probability")
    @patch("src.scanner.market_scanner.prefetch_ensemble_data")
    @patch("src.scanner.market_scanner.get")
    def test_total_timeout_remaining_events_fallback(
        self, mock_get, mock_prefetch, mock_dual
    ):
        """总超时后剩余事件全部 fallback。"""

        def config_side_effect(key):
            return {
                "scanner.strike_optimizer_total_timeout_sec": 0.001,  # 极短总超时
                "scanner.strike_optimizer_event_timeout_sec": 90,
                "scanner.max_weather_strike_opt_events": 100,
                "weather_city_scanner": {"enabled": False},
            }.get(key)

        mock_get.side_effect = config_side_effect

        def slow_dual(market):
            time.sleep(0.01)
            return {"probability": 0.15, "confidence": 0.80}

        mock_dual.side_effect = slow_dual

        # 3 events
        candidates = []
        for city in ["A", "B", "C"]:
            slug = f"{city}-slug"
            for j in range(3):
                candidates.append(
                    _make_weather_market(
                        f"{city}-{j}", f"Will {city} be {70+j}°F?", slug, score=60
                    )
                )

        with patch("src.scanner.market_scanner.get") as mock_get2:
            mock_get2.side_effect = config_side_effect
            scanner = MarketScanner.__new__(MarketScanner)
            result = scanner._weather_strike_optimizer(candidates)

        # All 3 events should have results
        assert len(result) == 3

        # At least some should be fallback (total timeout is 0.001s)
        fallbacks = [m for m in result if m.get("_strike_source") == "timeout_fallback"]
        assert len(fallbacks) >= 1


class TestStrikeOptConfigDefaults:
    """T290: 新增配置项有合理默认值。"""

    def test_config_keys_exist(self):
        from src.utils.config_loader import get

        scanner_cfg = get("scanner")
        assert "strike_optimizer_total_timeout_sec" in scanner_cfg
        assert "strike_optimizer_event_timeout_sec" in scanner_cfg
        assert "max_weather_strike_opt_events" in scanner_cfg

    def test_total_timeout_value(self):
        from src.utils.config_loader import get

        val = float(get("scanner.strike_optimizer_total_timeout_sec"))
        assert 60 <= val <= 600, f"total_timeout={val} should be 60-600s"

    def test_event_timeout_value(self):
        from src.utils.config_loader import get

        val = float(get("scanner.strike_optimizer_event_timeout_sec"))
        assert 30 <= val <= 300, f"event_timeout={val} should be 30-300s"

    def test_max_events_value(self):
        from src.utils.config_loader import get

        val = int(get("scanner.max_weather_strike_opt_events"))
        assert 5 <= val <= 50, f"max_events={val} should be 5-50"
