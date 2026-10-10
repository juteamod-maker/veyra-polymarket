"""SmartCapitalEngine — 基于余额Tier的统一资金缩放引擎 (T70)。

根据当前余额自动匹配 tier，统一输出：
  - max_bet_pct: 最大单笔占比
  - category_multiplier: 赛道乘数（高风险赛道在小余额时惩罚更大）

所有参数读取 config.json → smart_capital 节，热加载。
enabled=false 时不干预，由调用方回退到原有参数。
"""

import logging

from src.utils.config_loader import get

logger = logging.getLogger(__name__)

_DEFAULT_TIERS = [
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
]


class SmartCapitalEngine:
    """根据余额Tier统一管控仓位缩放。"""

    @staticmethod
    def is_enabled() -> bool:
        """SCE 是系统基建层，永远启用。不依赖 config 开关。"""
        return True

    @staticmethod
    def _get_tiers() -> list[dict]:
        cfg = get("smart_capital") or {}
        tiers = cfg.get("tiers")
        if not tiers or not isinstance(tiers, list):
            return _DEFAULT_TIERS
        return tiers

    @staticmethod
    def _match_tier(balance: float) -> dict:
        """匹配余额对应的 tier（从小到大，第一个 max_balance > balance 的层）。"""
        tiers = SmartCapitalEngine._get_tiers()
        for tier in tiers:
            if balance <= float(tier.get("max_balance", 999999)):
                return tier
        return tiers[-1] if tiers else _DEFAULT_TIERS[-1]

    @staticmethod
    def get_max_bet_pct(balance: float) -> float:
        """当前余额下的最大单笔比例。"""
        tier = SmartCapitalEngine._match_tier(balance)
        return float(tier.get("max_bet_pct", 0.08))

    @staticmethod
    def get_category_multiplier(balance: float, category: str) -> float:
        """赛道仓位乘数（小余额时高风险赛道惩罚更大）。"""
        tier = SmartCapitalEngine._match_tier(balance)
        mults = tier.get("category_multipliers") or {}
        return float(mults.get(category, mults.get("default", 1.0)))

    @staticmethod
    def get_cat_max_usdc(balance: float, category: str) -> float:
        """赛道绝对最大下注额 = balance × max_bet_pct × category_multiplier。

        hard_max 统一从 paper_trading.hard_max_bet_usdc 读取（单一来源）。
        """
        pct = SmartCapitalEngine.get_max_bet_pct(balance)
        mult = SmartCapitalEngine.get_category_multiplier(balance, category)
        pt_cfg = get("paper_trading") or {}
        hard_max = float(pt_cfg.get("hard_max_bet_usdc", 25.0))
        return min(balance * pct * mult, hard_max)

    @staticmethod
    def scale_kelly_size(
        kelly_size: float,
        balance: float,
        category: str,
    ) -> float:
        """对 Kelly 计算出的原始 size 应用 tier 缩放。

        Returns:
            调整后的 size（USDC），已 cap 到 cat_max 和 min_bet。
        """
        cat_max = SmartCapitalEngine.get_cat_max_usdc(balance, category)
        min_bet = float(get("executor.min_bet_usdc") or 2.0)
        size = min(kelly_size, cat_max)
        size = max(size, min_bet)
        return round(size, 2)

    @staticmethod
    def get_tier_name(balance: float) -> str:
        """返回当前余额所属 tier 名称。"""
        tier = SmartCapitalEngine._match_tier(balance)
        return tier.get("name", "unknown")
