"""
exit_policy — 可组合退出策略链
ExitPolicy 框架，将退出逻辑从 outcome_tracker 的内联条件分支
重构为独立策略模块，支持 strategy_tag 联动 hold/exit 决策。
"""

from src.exit_policy.base import (
    BetContext,
    ExitAction,
    ExitAdvice,
    ExitPolicy,
    PolicyChain,
)

__all__ = ["BetContext", "ExitAction", "ExitAdvice", "ExitPolicy", "PolicyChain"]
__all__ = ["BetContext", "ExitAction", "ExitAdvice", "ExitPolicy", "PolicyChain"]
