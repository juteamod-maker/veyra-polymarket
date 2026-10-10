"""
exit_policy.base — ExitPolicy 抽象基类和 PolicyChain 评估器

设计说明：
  - ExitPolicy 作为"顾问层"嵌入到 outcome_tracker 现有退出逻辑中
  - PolicyChain.should_hold() 在触发退出之前被调用：
    → 返回 ExitAdvice(HOLD) 时阻止该次退出
    → 返回 None 时不干预，让现有逻辑正常执行
  - 不替换 outcome_tracker 的触发检测逻辑，仅在触发后、执行前增设"HOLD 门控"
"""

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class ExitAction(Enum):
    """策略建议的动作。"""

    HOLD = "hold"  # 阻止退出，继续持有
    SKIP = "skip"  # 无意见，交给下一个策略判断


@dataclass
class ExitAdvice:
    """策略评估结果。"""

    action: ExitAction
    reason: str
    policy_name: str


@dataclass
class BetContext:
    """单笔投注的退出评估上下文。

    所有退出策略共享此数据结构，outcome_tracker 在触发退出前构建。
    """

    bet: dict  # 完整 DB 行
    current_price: float  # 当前卖出价
    entry_price: float  # 入场价
    return_pct: float  # (current - entry) / entry
    category: str  # e.g., "weather", "sports"
    outcome: str  # "YES" / "NO"
    hours_to_expiry: Optional[float] = None  # 距到期小时数（可能为 None）
    ai_probability: float = 0.0  # P(YES)，入场时 AI 给出
    trigger_type: str = ""  # "profit_take" / "stop_loss"
    trigger_name: str = ""  # e.g., "trailing", "rotation", "standard_sl", "floor"
    strategy_tag: Optional[str] = None  # DB 中的 strategy_tag
    strategy_params: dict = field(default_factory=dict)  # JSON 解析后的参数

    @classmethod
    def from_bet(
        cls,
        bet: dict,
        current_price: float,
        hours_to_expiry: Optional[float],
        trigger_type: str,
        trigger_name: str,
    ) -> "BetContext":
        """从 DB 行和运行时数据构建 BetContext。"""
        entry_price = float(bet.get("market_price") or 0)
        return_pct = (
            (current_price - entry_price) / entry_price if entry_price > 0 else 0.0
        )
        # 解析 strategy_params JSON
        raw_params = bet.get("strategy_params")
        params: dict = {}
        if raw_params and isinstance(raw_params, str):
            try:
                params = json.loads(raw_params)
            except (json.JSONDecodeError, TypeError):
                pass
        elif isinstance(raw_params, dict):
            params = raw_params

        return cls(
            bet=bet,
            current_price=current_price,
            entry_price=entry_price,
            return_pct=return_pct,
            category=(bet.get("category") or "").lower(),
            outcome=(bet.get("outcome") or "").strip().upper(),
            hours_to_expiry=hours_to_expiry,
            ai_probability=float(bet.get("ai_probability") or 0),
            trigger_type=trigger_type,
            trigger_name=trigger_name,
            strategy_tag=bet.get("strategy_tag"),
            strategy_params=params,
        )


class ExitPolicy(ABC):
    """退出策略基类。

    每个策略实现 evaluate()：
      - 返回 ExitAdvice(HOLD) → 阻止本次退出
      - 返回 None → 不干预，让 PolicyChain 继续检查下一个策略
    """

    # 优先级：数字越小越先执行（先发言权越大）
    priority: int = 50

    @abstractmethod
    def evaluate(self, ctx: BetContext, config_fn=None) -> Optional[ExitAdvice]:
        """评估是否应该 hold 住这笔退出。"""
        ...


class PolicyChain:
    """按优先级顺序评估多个 ExitPolicy，返回第一个 HOLD 建议。"""

    def __init__(self, policies: list[ExitPolicy]) -> None:
        self._policies = sorted(policies, key=lambda p: p.priority)

    def should_hold(self, ctx: BetContext, config_fn=None) -> Optional[ExitAdvice]:
        """遍历所有策略，返回第一个 HOLD 建议；无则返回 None。

        config_fn: 可选的配置读取函数，传递给策略的 evaluate()。
        当从 outcome_tracker 调用时传入模块级 get，供策略读取被 mock 的配置。
        """
        kwargs = {"config_fn": config_fn} if config_fn is not None else {}
        for policy in self._policies:
            try:
                advice = policy.evaluate(ctx, **kwargs)
                if advice is not None and advice.action == ExitAction.HOLD:
                    logger.debug(
                        f"[PolicyChain] {advice.policy_name}: HOLD — {advice.reason} "
                        f"| trigger={ctx.trigger_name} bet={ctx.bet.get('bet_id', '')[:8]}"
                    )
                    return advice
            except Exception as exc:
                logger.warning(
                    f"[PolicyChain] {type(policy).__name__}.evaluate 异常: {exc}"
                )
        return None

    @property
    def policy_names(self) -> list[str]:
        """返回当前链中所有策略名（用于日志/调试）。"""
        return [type(p).__name__ for p in self._policies]
