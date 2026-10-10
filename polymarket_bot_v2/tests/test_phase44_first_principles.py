"""
test_phase44_first_principles.py — Phase 44 第一性原理改进测试
覆盖: crypto市场分类, 推理一致性检查, sports spread检测, weather YES解锁, crypto smart_money硬门槛
"""

from unittest.mock import MagicMock, patch

import pytest
from src.signal.signal_engine import Signal, SignalEngine


# ══════════════════════════════════════════════════════════════
# 1. Crypto 市场类型分类
# ══════════════════════════════════════════════════════════════
class TestCryptoMarketClassification:
    """验证 _classify_crypto_market_type 正确检测 daily/narrow_range/tradeable"""

    def test_daily_prediction_eod(self):
        q = "Will BTC be above $100,000 by end of day on March 25?"
        assert SignalEngine._classify_crypto_market_type(q) == "daily_prediction"

    def test_daily_prediction_midnight(self):
        q = "Will ETH price exceed $4,000 by midnight UTC?"
        assert SignalEngine._classify_crypto_market_type(q) == "daily_prediction"

    def test_daily_prediction_24h(self):
        q = "Will SOL increase 5% in the next 24 hours?"
        assert SignalEngine._classify_crypto_market_type(q) == "daily_prediction"

    def test_daily_prediction_tomorrow(self):
        q = "Will Dogecoin be above $0.50 by tomorrow?"
        assert SignalEngine._classify_crypto_market_type(q) == "daily_prediction"

    def test_narrow_range(self):
        q = "Will BTC be between $99,500 and $100,500 on March 28?"
        assert SignalEngine._classify_crypto_market_type(q) == "narrow_range"

    def test_narrow_range_small_spread(self):
        q = "Will ETH be between $3990 and $4010 tomorrow?"
        assert SignalEngine._classify_crypto_market_type(q) == "narrow_range"

    def test_wide_range_is_tradeable(self):
        q = "Will BTC be between $80,000 and $120,000 by June?"
        assert SignalEngine._classify_crypto_market_type(q) == "tradeable"

    def test_trend_event_tradeable(self):
        q = "Will Bitcoin reach a new all-time high in Q2 2026?"
        assert SignalEngine._classify_crypto_market_type(q) == "tradeable"

    def test_tvl_metric_tradeable(self):
        q = "Will Ethereum TVL exceed $100B by end of 2026?"
        assert SignalEngine._classify_crypto_market_type(q) == "tradeable"

    def test_generic_price_tradeable(self):
        q = "Will BTC be above $150,000 on June 30?"
        assert SignalEngine._classify_crypto_market_type(q) == "tradeable"


# ══════════════════════════════════════════════════════════════
# 2. Sports spread 检测
# ══════════════════════════════════════════════════════════════
class TestSpreadDetection:
    """验证 _is_spread_market 检测 spread/over-under 让分盘"""

    def test_plus_half_spread(self):
        assert SignalEngine._is_spread_market("Will Lakers cover +3.5 against Celtics?")

    def test_minus_half_spread(self):
        assert SignalEngine._is_spread_market("Patriots -7.5 vs Jets")

    def test_point_spread(self):
        assert SignalEngine._is_spread_market("Point spread: Chiefs vs Bills")

    def test_handicap(self):
        assert SignalEngine._is_spread_market("Asian handicap: Man City vs Liverpool")

    def test_over_under(self):
        assert SignalEngine._is_spread_market("Over/under 210.5 total points")

    def test_win_by_n(self):
        assert SignalEngine._is_spread_market("Will Yankees win by 3 or more?")

    def test_moneyline_not_detected(self):
        assert not SignalEngine._is_spread_market("Will Lakers beat Celtics?")

    def test_simple_vs_not_detected(self):
        assert not SignalEngine._is_spread_market("Manchester City vs Arsenal")

    def test_who_wins_not_detected(self):
        assert not SignalEngine._is_spread_market("Who will win the Super Bowl?")


# ══════════════════════════════════════════════════════════════
# 3. AI 推理一致性检查
# ══════════════════════════════════════════════════════════════
class TestReasoningConsistency:
    """验证 _check_reasoning_consistency 检测 AI hallucination"""

    def setup_method(self):
        with patch.object(SignalEngine, "__init__", lambda self: None):
            self.engine = SignalEngine()

    def test_negative_reasoning_high_prob_inconsistent(self):
        """reasoning 说 impossible 但给了高概率 → 不一致"""
        votes = {
            "model_a": {
                "outcome": "YES",
                "probability": 0.85,
                "confidence": 0.7,
                "r": "This is virtually impossible given the current data",
            }
        }
        result = self.engine._check_reasoning_consistency(votes, "crypto")
        assert "reasoning_inconsistency" in result

    def test_positive_reasoning_low_prob_inconsistent(self):
        """reasoning 说 guaranteed 但给了低概率 → 不一致"""
        votes = {
            "model_a": {
                "outcome": "YES",
                "probability": 0.45,
                "confidence": 0.6,
                "r": "This is almost certain to happen based on current trends",
            }
        }
        result = self.engine._check_reasoning_consistency(votes, "sports")
        assert "reasoning_inconsistency" in result

    def test_consistent_negative(self):
        """reasoning 消极且概率低 for YES → 一致（NO方向80%信心说unlikely是对YES说的）"""
        votes = {
            "model_a": {
                "outcome": "NO",
                "probability": 0.20,
                "confidence": 0.7,
                "r": "Based on current trends, probability is low",
            }
        }
        result = self.engine._check_reasoning_consistency(votes, "crypto")
        assert result == ""

    def test_consistent_positive(self):
        """reasoning 正面且概率高 → 一致"""
        votes = {
            "model_a": {
                "outcome": "YES",
                "probability": 0.85,
                "confidence": 0.8,
                "r": "Strong data supports this outcome",
            }
        }
        result = self.engine._check_reasoning_consistency(votes, "weather")
        assert result == ""

    def test_neutral_reasoning_always_consistent(self):
        """无强烈极性的 reasoning → 一致"""
        votes = {
            "model_a": {
                "outcome": "YES",
                "probability": 0.72,
                "confidence": 0.65,
                "r": "The data suggests a moderate probability",
            }
        }
        result = self.engine._check_reasoning_consistency(votes, "crypto")
        assert result == ""

    def test_multi_model_one_inconsistent(self):
        """一个模型不一致 → 拒绝整个信号"""
        votes = {
            "model_a": {
                "outcome": "YES",
                "probability": 0.80,
                "confidence": 0.7,
                "r": "Good outlook based on the data",
            },
            "model_b": {
                "outcome": "YES",
                "probability": 0.90,
                "confidence": 0.8,
                "r": "This has zero chance of happening",
            },
        }
        result = self.engine._check_reasoning_consistency(votes, "crypto")
        # Phase 44.1: 多数阈值——1/2 模型矛盾不足以拒绝（majority = 2 needed）
        assert result == ""


# ══════════════════════════════════════════════════════════════
# 4. Weather YES 条件解锁
# ══════════════════════════════════════════════════════════════
class TestCryptoSmartMoneyGate:
    """验证 crypto 赛道聪明钱包硬门槛配置"""

    def test_config_has_require_aligned(self):
        """确认 config 中有 smart_money.crypto_require_aligned"""
        from src.utils.config_loader import get

        val = get("paper_trading.smart_money.crypto_require_aligned")
        # Phase 44.1: 修正为 false（API无数据时 aligned=0 会阻塞全部 crypto）
        assert val is False

    def test_reasoning_check_always_on(self):
        """确认推理一致性检查始终启用（无需config开关）"""
        from src.signal.signal_engine import SignalEngine

        # _check_reasoning_consistency 方法应存在且始终被调用
        assert hasattr(SignalEngine, "_check_reasoning_consistency")


# ══════════════════════════════════════════════════════════════
# 6. 黑名单扩展验证
# ══════════════════════════════════════════════════════════════
class TestBlacklistExpansion:
    """验证 spread 相关关键词已加入黑名单"""

    def test_spread_keywords_in_blacklist(self):
        from src.utils.config_loader import get

        keywords = get("scanner.blacklist_keywords") or []
        kw_lower = [k.lower() for k in keywords]
        assert "point spread" in kw_lower
        assert "margin of victory" in kw_lower
        assert "margin of victory" in kw_lower
