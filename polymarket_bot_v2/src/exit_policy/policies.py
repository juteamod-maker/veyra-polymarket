"""
exit_policy.policies — 具体退出策略实现

策略清单（按优先级）：
  1. WeatherConfidenceHoldPolicy (pri=10)  — T269: 天气NO高置信持有到结算
  2. CityNoScoreHoldPolicy       (pri=20)  — 城市扫描高分NO加强持有
"""

import logging
from typing import Optional

from src.exit_policy.base import BetContext, ExitAction, ExitAdvice, ExitPolicy
from src.utils.config_loader import get

logger = logging.getLogger(__name__)

# 这些 trigger 无论什么策略都不被 hold（系统级强制退出）
_UNBLOCKABLE_PT_TRIGGERS = frozenset({"certainty_sell"})
_UNBLOCKABLE_SL_TRIGGERS = frozenset({"floor", "live_floor", "effective_resolution"})


class WeatherConfidenceHoldPolicy(ExitPolicy):
    """T269: 天气NO高置信仓位 — 持有到结算保护。

    条件：
      - 天气赛道 + NO方向
      - AI P(YES) > 0 且 < max_ai_prob（高置信 NO 方向正确）
      - 距离到期 < hours_before_expiry
    效果：
      - 止盈侧：跳过所有止盈（certainty_sell 除外）
      - 止损侧：跳过所有止损（floor/effective_resolution 除外）
    """

    priority = 10

    def evaluate(self, ctx: BetContext, config_fn=None) -> Optional[ExitAdvice]:
        _get = config_fn or get
        cfg = _get("weather_confidence_hold") or {}
        if not cfg.get("enabled", False):
            return None

        max_prob = float(cfg.get("max_ai_prob", 0.25))
        max_hours = float(cfg.get("hours_before_expiry", 24))

        # 仅天气NO仓位
        if ctx.category != "weather" or ctx.outcome != "NO":
            return None

        # AI 概率必须有效且低（高置信NO）
        if ctx.ai_probability <= 0 or ctx.ai_probability >= max_prob:
            return None

        # 必须在到期窗口内
        # T211: 使用 < -2 而非 <= 0，防止天气市场 hours_to_expiry 因时区
        # 计算偏差为 0 或小负数时失效。天气市场常在结算前数小时才出结果，
        # -2h 容错窗口确保即使到期时间略有偏差也能正常保护。
        if ctx.hours_to_expiry is None or ctx.hours_to_expiry < -2:
            return None
        if ctx.hours_to_expiry >= max_hours:
            return None

        # 不阻止系统级强制退出
        if (
            ctx.trigger_type == "profit_take"
            and ctx.trigger_name in _UNBLOCKABLE_PT_TRIGGERS
        ):
            return None
        if (
            ctx.trigger_type == "stop_loss"
            and ctx.trigger_name in _UNBLOCKABLE_SL_TRIGGERS
        ):
            return None

        return ExitAdvice(
            action=ExitAction.HOLD,
            reason=(
                f"天气NO ai_prob={ctx.ai_probability:.3f}<{max_prob} "
                f"距到期{ctx.hours_to_expiry:.1f}h → 持有到$1.00结算"
            ),
            policy_name="WeatherConfidenceHold",
        )


class CityNoScoreHoldPolicy(ExitPolicy):
    """城市扫描策略高分NO仓位 — 加强持有保护。

    条件：
      - strategy_tag == "city_no"
      - 距离到期 < hold_hours（默认 24h）
    效果：
      - score >= strong_hold_min_score (0.7): 阻止 trailing/rotation/standard 退出
      - score >= mild_hold_min_score (0.5): 阻止 trailing/rotation 退出
    """

    priority = 20

    # 可被此策略阻止的 trigger（非强制）
    _MILD_HOLDABLE = frozenset({"trailing", "rotation"})
    _STRONG_HOLDABLE = frozenset(
        {"trailing", "rotation", "standard_sl", "live_standard_sl"}
    )

    def evaluate(self, ctx: BetContext, config_fn=None) -> Optional[ExitAdvice]:
        _get = config_fn or get
        cfg = _get("weather_city_scanner") or {}
        if not cfg.get("enabled", False):
            return None

        hold_cfg = cfg.get("score_hold") or {}
        if not hold_cfg.get("enabled", True):
            return None

        # 仅城市NO策略仓位（含T106 weather_right_tail 信号）
        _tag = ctx.strategy_tag or ""
        if _tag not in ("city_no", "t268_city_no") and not _tag.startswith(
            ("weather_right_tail", "weather_double_no")
        ):
            return None

        # 必须在到期窗口内
        # T212: 使用 < -2 与 WeatherConfidenceHoldPolicy 保持一致，
        # 防止天气市场 hours_to_expiry 因时区计算偏差为 0 或小负数时策略失效
        max_hours = float(hold_cfg.get("hold_hours", 24))
        if ctx.hours_to_expiry is None or ctx.hours_to_expiry < -2:
            return None
        if ctx.hours_to_expiry >= max_hours:
            return None

        # 不阻止系统级强制退出
        if (
            ctx.trigger_type == "profit_take"
            and ctx.trigger_name in _UNBLOCKABLE_PT_TRIGGERS
        ):
            return None
        if (
            ctx.trigger_type == "stop_loss"
            and ctx.trigger_name in _UNBLOCKABLE_SL_TRIGGERS
        ):
            return None

        score = float(ctx.strategy_params.get("score", 0))
        strong_min = float(hold_cfg.get("strong_hold_min_score", 0.7))
        mild_min = float(hold_cfg.get("mild_hold_min_score", 0.5))

        if score >= strong_min and ctx.trigger_name in self._STRONG_HOLDABLE:
            return ExitAdvice(
                action=ExitAction.HOLD,
                reason=(
                    f"CityNO score={score:.2f}≥{strong_min} 强持有, "
                    f"阻止 {ctx.trigger_name}"
                ),
                policy_name="CityNoScoreHold",
            )

        if score >= mild_min and ctx.trigger_name in self._MILD_HOLDABLE:
            return ExitAdvice(
                action=ExitAction.HOLD,
                reason=(
                    f"CityNO score={score:.2f}≥{mild_min} 温和持有, "
                    f"阻止 {ctx.trigger_name}"
                ),
                policy_name="CityNoScoreHold",
            )

        return None
