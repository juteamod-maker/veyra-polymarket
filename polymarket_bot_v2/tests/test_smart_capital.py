"""SmartCapitalEngine 单元测试 (T70)。"""

from unittest.mock import patch

import pytest

# 测试用 config
_MOCK_CONFIG = {
    "smart_capital": {
        "enabled": True,
        "tiers": [
            {
                "name": "micro",
                "max_balance": 10,
                "max_bet_pct": 0.12,
                "category_multipliers": {
                    "weather": 1.0,
                    "sports": 0.7,
                    "crypto": 0.3,
                    "default": 0.5,
                },
            },
            {
                "name": "small",
                "max_balance": 50,
                "max_bet_pct": 0.10,
                "category_multipliers": {
                    "weather": 1.0,
                    "sports": 0.8,
                    "crypto": 0.5,
                    "default": 0.7,
                },
            },
            {
                "name": "medium",
                "max_balance": 200,
                "max_bet_pct": 0.08,
                "category_multipliers": {
                    "weather": 1.0,
                    "sports": 0.9,
                    "crypto": 0.6,
                    "default": 0.8,
                },
            },
            {
                "name": "large",
                "max_balance": 999999,
                "max_bet_pct": 0.06,
                "category_multipliers": {
                    "weather": 1.0,
                    "sports": 1.0,
                    "crypto": 0.7,
                    "default": 0.9,
                },
            },
        ],
        "pause": {
            "enabled": True,
            "balance_floor_usdc": 5.0,
            "balance_floor_pct_of_peak": 0.05,
        },
    },
    "executor": {"min_bet_usdc": 1.0},
    "paper_trading": {"hard_max_bet_usdc": 25.0},
}

_DISABLED_CONFIG = {
    "smart_capital": {"enabled": False},
    "executor": {"min_bet_usdc": 1.0},
    "paper_trading": {"hard_max_bet_usdc": 25.0},
}


def _mock_get(key, fallback=None):
    """模拟 config_loader.get() 的点分路径查找。"""
    parts = key.split(".")
    val = _MOCK_CONFIG
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p)
        else:
            return fallback
        if val is None:
            return fallback
    return val


def _mock_get_disabled(key, fallback=None):
    parts = key.split(".")
    val = _DISABLED_CONFIG
    for p in parts:
        if isinstance(val, dict):
            val = val.get(p)
        else:
            return fallback
        if val is None:
            return fallback
    return val


class TestTierMatching:
    """Tier 匹配测试。"""

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_micro_tier(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        assert SmartCapitalEngine.get_tier_name(5.0) == "micro"
        assert SmartCapitalEngine.get_tier_name(10.0) == "micro"

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_small_tier(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        assert SmartCapitalEngine.get_tier_name(11.0) == "small"
        assert SmartCapitalEngine.get_tier_name(50.0) == "small"

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_medium_tier(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        assert SmartCapitalEngine.get_tier_name(100.0) == "medium"

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_large_tier(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        assert SmartCapitalEngine.get_tier_name(500.0) == "large"


class TestMaxBetPct:
    """最大单笔比例测试。"""

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_micro_gets_higher_pct(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        assert SmartCapitalEngine.get_max_bet_pct(5.0) == 0.12

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_large_gets_lower_pct(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        assert SmartCapitalEngine.get_max_bet_pct(500.0) == 0.06


class TestCategoryMultiplier:
    """赛道乘数测试。"""

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_micro_crypto_heavy_penalty(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        assert SmartCapitalEngine.get_category_multiplier(5.0, "crypto") == 0.3

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_large_crypto_mild_penalty(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        assert SmartCapitalEngine.get_category_multiplier(500.0, "crypto") == 0.7

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_weather_always_1(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        assert SmartCapitalEngine.get_category_multiplier(5.0, "weather") == 1.0
        assert SmartCapitalEngine.get_category_multiplier(500.0, "weather") == 1.0

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_unknown_category_uses_default(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        assert SmartCapitalEngine.get_category_multiplier(5.0, "geopolitics") == 0.5


class TestCatMaxUsdc:
    """赛道最大下注额测试。"""

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_micro_weather(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        # 5.0 × 0.12 × 1.0 = 0.60
        result = SmartCapitalEngine.get_cat_max_usdc(5.0, "weather")
        assert abs(result - 0.60) < 0.01

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_micro_crypto(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        # 5.0 × 0.12 × 0.3 = 0.18
        result = SmartCapitalEngine.get_cat_max_usdc(5.0, "crypto")
        assert abs(result - 0.18) < 0.01

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_hard_max_cap(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        # 10000 × 0.06 × 1.0 = 600, but hard_max = 25
        result = SmartCapitalEngine.get_cat_max_usdc(10000, "weather")
        assert result == 25.0


class TestScaleKellySize:
    """Kelly 缩放测试。"""

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_caps_to_cat_max(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        # cat_max for micro/weather = 0.60, kelly=5.0 → capped to 0.60
        result = SmartCapitalEngine.scale_kelly_size(5.0, 5.0, "weather")
        assert abs(result - 1.0) < 0.01  # min_bet=1.0 floor

    @patch("src.utils.smart_capital.get", side_effect=_mock_get)
    def test_preserves_small_kelly(self, _):
        from src.utils.smart_capital import SmartCapitalEngine

        # cat_max for medium/weather = 200*0.08*1.0=16, kelly=3.0
        result = SmartCapitalEngine.scale_kelly_size(3.0, 200.0, "weather")
        assert abs(result - 3.0) < 0.01


class TestEnabled:
    """SCE 基建层测试 — 永远启用。"""

    def test_always_enabled(self):
        from src.utils.smart_capital import SmartCapitalEngine

        assert SmartCapitalEngine.is_enabled()
        assert SmartCapitalEngine.is_enabled()  # 多次调用一致
