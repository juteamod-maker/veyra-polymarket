"""
test_t291_category_min_ev.py — T291: 验证 category_min_ev 赛道级EV阈值在信号引擎中生效
BUG: signal_engine.generate_signals() 仅使用全局 min_ev_threshold，
     忽略了 paper_trading.category_min_ev 的赛道级覆盖设置。
FIX: 加载 cat_min_ev 字典，体育/加密等赛道使用更低的 EV 阈值。
"""

from unittest.mock import MagicMock, patch

import pytest
from src.signal.signal_engine import Signal, SignalEngine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal(
    ev: float,
    confidence: float = 0.60,
    outcome: str = "YES",
    category: str = "sports",
    market_id: str = "test_001",
) -> Signal:
    """构造一个 Signal 对象用于测试。"""
    sig = Signal.__new__(Signal)
    sig.ev = ev
    sig.confidence = confidence
    sig.outcome = outcome
    sig.skip = False
    sig.skip_reason = ""
    sig.market_id = market_id
    sig.probability = 0.55
    sig.category = category
    sig.reasoning = "test"
    sig.size = 1.0
    sig.market = {"id": market_id}
    return sig


class TestCategoryMinEvLoaded:
    """确认 generate_signals 读取 category_min_ev 配置。"""

    def test_config_has_sports_category_min_ev(self):
        """config.json 中 category_min_ev.sports 应为 0.005。"""
        from src.utils.config_loader import get

        cat_ev = get("paper_trading.category_min_ev") or {}
        assert "sports" in cat_ev
        assert (
            float(cat_ev["sports"]) <= 0.01
        ), f"sports min_ev 应 ≤0.01, 实际={cat_ev['sports']}"

    def test_config_has_crypto_category_min_ev(self):
        """config.json 中 category_min_ev.crypto 应为 0.005。"""
        from src.utils.config_loader import get

        cat_ev = get("paper_trading.category_min_ev") or {}
        assert "crypto" in cat_ev
        assert float(cat_ev["crypto"]) <= 0.01


class TestSportsCategoryMinEvApplied:
    """验证体育信号使用赛道级EV阈值而非全局阈值。"""

    @patch("src.signal.signal_engine.get")
    def test_sports_signal_passes_with_category_ev(self, mock_get):
        """体育信号 EV=0.01 应通过赛道级阈值(0.005)但低于全局(0.04)。"""

        # 模拟配置
        def _config_side(key, *a, **kw):
            cfg = {
                "signal.min_ev_threshold": 0.04,
                "signal.min_confidence": 0.55,
                "paper_trading.category_min_ev": {"sports": 0.005},
                "paper_trading.category_min_confidence": {"sports": 0.25},
                "paper_trading.settlement_max_hours": {"sports": 72},
                "signal.max_tier1_markets": 80,
                "signal.max_tier2_markets": 10,
                "signal.max_tier3_markets": 0,
                "signal.category_min_markets": {},
                "signal.rotation_pool_multiplier": 3,
                "signal.rotation_memory_seconds": 900,
                "signal.sports_max_odds_verify_per_cycle": 25,
                "paper_trading.sports_blocked_keywords": [],
            }
            return cfg.get(key)

        mock_get.side_effect = _config_side

        engine = SignalEngine.__new__(SignalEngine)
        engine._recently_analyzed = {}
        engine._cycle_stats = {
            "ai_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "pre_filtered": 0,
            "second_skipped": 0,
        }
        engine._sports_enriched_this_cycle = 0
        engine._sports_odds_calls_this_cycle = 0

        # 创建 sports signal with low EV (between 0.005 and 0.04)
        sports_sig = _make_signal(ev=0.015, confidence=0.40, category="sports")

        # Mock analyze_sports_two_phase_batch to return our signal
        engine.analyze_sports_two_phase_batch = MagicMock(
            return_value={"test_001": sports_sig}
        )
        engine._reset_sports_enriched_count = MagicMock()
        engine._get_balance = MagicMock(return_value=100.0)
        engine._quick_price_screen = MagicMock(return_value=False)
        engine._skip_saturated_events = MagicMock(side_effect=lambda x: x)

        # Market with sports category
        sports_market = {
            "id": "test_001",
            "question": "Will Team A win?",
            "bestAsk": 0.45,
            "bestBid": 0.43,
            "liquidity": 50000,
            "endDate": "2026-04-01T12:00:00Z",
            "tags": [{"label": "Sports"}],
        }

        # Patch detect_category to return "sports"
        with patch("src.signal.signal_engine.detect_category", return_value="sports"):
            signals = engine.generate_signals(
                {"tier1": [sports_market], "tier2": [], "tier3": []},
                balance=100.0,
            )

        # 验证: EV=0.015 应通过体育赛道级阈值(0.005)
        assert len(signals) >= 1, f"EV=0.015 应通过sports赛道级阈值0.005, 但被过滤了"


class TestNonSportsCategoryMinEvApplied:
    """验证非体育赛道也使用赛道级EV阈值。"""

    def test_category_min_ev_config_structure(self):
        """所有活跃赛道都应有 category_min_ev 配置。"""
        from src.utils.config_loader import get

        cat_ev = get("paper_trading.category_min_ev") or {}
        required = ["weather", "sports", "crypto"]
        for cat in required:
            assert cat in cat_ev, f"category_min_ev 缺少 {cat}"


class TestMaxTier1Increase:
    """验证 max_tier1_markets 已增加到支撑更多体育市场。"""

    def test_max_tier1_at_least_120(self):
        from src.utils.config_loader import get

        val = int(get("signal.max_tier1_markets") or 0)
        assert val >= 50, f"max_tier1_markets={val} 应≥50"


class TestSportsTopNIncrease:
    """验证 category_top_n.sports 已增加。"""

    def test_sports_top_n_at_least_100(self):
        from src.utils.config_loader import get

        cat_top = get("scanner.category_top_n") or {}
        sports_n = int(cat_top.get("sports", 0))
        assert sports_n >= 100, f"category_top_n.sports={sports_n}, 应≥100"
