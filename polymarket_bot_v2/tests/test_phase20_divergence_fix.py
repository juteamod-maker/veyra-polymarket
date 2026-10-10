"""
test_phase20_divergence_fix.py — Phase 20 修复验证
覆盖:
  1. _check_insufficient_data 新增模式匹配（market_id=1632105 教训）
  2. 完整信号链：One Piece Season 2 场景复现

BUG 背景:
  market_id=1632105 "One Piece Season 2" — negRisk 多选市场，市场 YES=83%，
  AI 三模型均无数据默认 P(YES)=15% → 系统买了 NO → 方向完全错误。
  根因: _check_insufficient_data 遗漏 qwen3 "No recent news"
  (注: market_divergence_protection 已删除 — 三赛道全豁免，属于死代码)
"""

from unittest.mock import MagicMock, patch

import pytest
from src.signal.signal_engine import Signal, SignalEngine
from src.utils.config_loader import get

# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _build_engine() -> SignalEngine:
    """创建 SignalEngine 实例（跳过 OpenAI/OpenRouter 初始化）。"""
    engine = SignalEngine.__new__(SignalEngine)
    engine._evolver = None
    engine.smart_money = MagicMock()
    engine.smart_money.get_smart_context_for_market.return_value = ""
    engine.client = MagicMock()
    engine._cycle_stats = {"ai_calls": 0, "pre_filtered": 0, "second_skipped": 0}
    engine._recently_analyzed = {}
    return engine


# ===========================================================================
# 1. _check_insufficient_data 新模式匹配
# ===========================================================================


class TestInsufficientDataNewPatterns:
    """Phase 20: 确保新增的无数据模式能被正确检测。"""

    def setup_method(self):
        self.engine = _build_engine()

    def test_qwen3_no_recent_news_detected(self):
        """qwen3 的 'No recent news' 应匹配 'no recent' 模式。"""
        votes = {
            "qwen3": {
                "outcome": "NO",
                "probability": 0.15,
                "confidence": 0.7,
                "r": "No recent news or announcements about 'One Piece: Season 2' in the provided data",
            },
            "gemini": {
                "outcome": "NO",
                "probability": 0.12,
                "confidence": 0.65,
                "r": "The provided news articles do not contain any information about One Piece",
            },
            "deepseek": {
                "outcome": "NO",
                "probability": 0.18,
                "confidence": 0.7,
                "r": "The provided news headlines are unrelated to One Piece, offering no evidence",
            },
        }
        result = self.engine._check_insufficient_data(votes, "1632105")
        assert result != "", "应检测到全部模型数据不足"
        assert "3/3" in result

    def test_single_no_recent_pattern(self):
        """单独测试 'no recent' 模式匹配。"""
        votes = {
            "model_a": {
                "r": "no recent data available for this topic",
            },
            "model_b": {
                "r": "no relevant information found",
            },
        }
        result = self.engine._check_insufficient_data(votes, "test_id")
        assert result != "", "'no recent' 和 'no relevant' 都应匹配"

    def test_unrelated_to_pattern(self):
        """'unrelated to' 应被检测为无数据。"""
        votes = {
            "model_a": {"r": "The articles are unrelated to the topic"},
            "model_b": {"r": "No mention of the subject in any source"},
        }
        result = self.engine._check_insufficient_data(votes, "test_id")
        assert result != "", "'unrelated to' 和 'no mention' 都应匹配"

    def test_are_unrelated_pattern(self):
        """'are unrelated' 模式匹配。"""
        votes = {
            "model_a": {"r": "The headlines are unrelated to Netflix"},
            "model_b": {"r": "There is no indication of any streaming data"},
        }
        result = self.engine._check_insufficient_data(votes, "test_id")
        assert result != "", "'are unrelated' 和 'no indication' 都应匹配"

    def test_no_news_pattern(self):
        """'no news' 模式匹配。"""
        votes = {
            "model_a": {"r": "There is no news about this event"},
            "model_b": {"r": "Unable to assess likelihood based on provided data"},
        }
        result = self.engine._check_insufficient_data(votes, "test_id")
        assert result != "", "'no news' 和 'unable to assess' 都应匹配"

    def test_single_model_not_judged(self):
        """单模型场景不判定（可能是 CostSave 只跑了一个）。"""
        votes = {
            "model_a": {"r": "no recent data"},
        }
        result = self.engine._check_insufficient_data(votes, "test_id")
        assert result == "", "单模型不判定"

    def test_mixed_models_not_skipped(self):
        """如果有模型提供了有效推理，不应触发跳过。"""
        votes = {
            "model_a": {"r": "no recent data available"},
            "model_b": {
                "r": "Based on Netflix viewership trends, One Piece leads by 25M hours"
            },
        }
        result = self.engine._check_insufficient_data(votes, "test_id")
        assert result == "", "有模型有数据时不应跳过"


# ===========================================================================
# 2. 完整场景复现: One Piece Season 2 (market_id=1632105)
# ===========================================================================


class TestOnePieceBugReproduction:
    """复现 market_id=1632105 完整信号链，验证两道防线都能拦截。"""

    def setup_method(self):
        self.engine = _build_engine()

    @patch("src.signal.signal_engine.get")
    @patch("src.signal.signal_engine.detect_category", return_value="pop_culture")
    def test_insufficient_data_blocks_one_piece(self, mock_cat, mock_get):
        """复现: 3个模型均无数据 → _check_insufficient_data 拦截（第一道防线）。"""
        mock_get.side_effect = lambda key, *a, **kw: {
            "signal.probability_calibration": {"enabled": False},
            "signal.bayesian_update": {"enabled": False},
            "signal.arbiter_model": "",
            "signal.skip_on_insufficient_data": True,
        }.get(key, get(key))

        # 模拟 AI 返回: 3模型全部无数据
        ai_responses = [
            {
                "outcome": "NO",
                "probability": 0.15,
                "confidence": 0.7,
                "r": "No recent news or announcements about One Piece Season 2 in the provided data",
            },
            {
                "outcome": "NO",
                "probability": 0.12,
                "confidence": 0.65,
                "r": "The provided news articles do not contain any information about One Piece Season 2",
            },
            {
                "outcome": "NO",
                "probability": 0.18,
                "confidence": 0.7,
                "r": "The provided news headlines are unrelated to One Piece, offering no evidence of a surge",
            },
        ]
        self.engine.client.chat.completions.create = MagicMock(
            side_effect=[
                MagicMock(
                    choices=[
                        MagicMock(message=MagicMock(content=str(r).replace("'", '"')))
                    ]
                )
                for r in ai_responses
            ]
        )

        market = {
            "id": "1632105",
            "question": 'Will "One Piece: Season 2" be the top global Netflix show this week?',
            "bestAsk": 0.867,
            "bestBid": 0.796,
            "outcomePrices": '["0.8315","0.1685"]',
            "description": "Netflix top show resolution",
            "negRisk": True,
        }

        sig = self.engine._analyze_market(market, balance=15.0)
        assert sig.skip is True, f"应跳过但未跳过: skip_reason={sig.skip_reason}"
        assert (
            "insufficient" in sig.skip_reason.lower()
        ), f"应因数据不足跳过，实际原因: {sig.skip_reason}"


# ===========================================================================
# 3. 边界情况
# ===========================================================================


class TestInsufficientDataEdgeCases:
    """_check_insufficient_data 边界情况测试。"""

    def setup_method(self):
        self.engine = _build_engine()

    def test_empty_reasoning_not_matched(self):
        """r 字段为空时不应误匹配。"""
        votes = {
            "model_a": {"r": ""},
            "model_b": {"r": ""},
        }
        result = self.engine._check_insufficient_data(votes, "test_id")
        assert result == "", "空推理不应触发无数据检测"

    def test_case_insensitive_matching(self):
        """大小写不敏感匹配。"""
        votes = {
            "model_a": {"r": "NO RECENT DATA available"},
            "model_b": {"r": "NO EVIDENCE of this event occurring"},
        }
        result = self.engine._check_insufficient_data(votes, "test_id")
        assert result != "", "大小写不敏感匹配应生效"

    def test_valid_data_not_false_positive(self):
        """有实质数据的推理不应被误判。"""
        votes = {
            "model_a": {
                "r": "Netflix Q1 2026 report shows One Piece at 35M view hours, trending #1 globally"
            },
            "model_b": {
                "r": "One Piece Season 2 launched March 15 and has dominated Netflix charts for 8 days"
            },
        }
        result = self.engine._check_insufficient_data(votes, "test_id")
        assert result == "", "有实质数据不应被误判为数据不足"

    def test_partial_match_allows_bet(self):
        """2个模型中1个无数据、1个有数据 → 允许投注。"""
        votes = {
            "model_a": {"r": "No recent news about this topic"},
            "model_b": {
                "r": "Netflix Top 10 data shows One Piece at #1 with 30M hours"
            },
        }
        result = self.engine._check_insufficient_data(votes, "test_id")
        assert result == "", "部分模型有数据时不应跳过"
