"""
v2_production_guard.py — VagusPoly V2 生产模式强制守卫
======================================================
V2 是 SaaS 多租户生产系统，永远只有 LIVE 模式，DRY_RUN 概念从 V2 中彻底移除。

【设计原则】
- DRY_RUN 在 V2 中不存在，is_dry_run() 永远返回 False
- 如果检测到 DRY_RUN=true 环境变量，立即 panic 并拒绝启动
- 如果检测到 config.json general.dry_run=true，立即 panic 并拒绝启动
- V2_MIGRATION_COMPLETE 是独立的迁移完成度控制，不是 DRY_RUN 的替代品

【V2_MIGRATION_COMPLETE 机制】
这是一个技术保障开关，与 DRY_RUN 完全无关：
- V2_MIGRATION_COMPLETE=false（默认）：执行层拒绝真实 CLOB 下单，仅记录审计日志
  → 在 Batch 2（order_executor 接入 V2BuilderAdapter）完成并验证后才设为 true
- V2_MIGRATION_COMPLETE=true：执行层正常下单
"""

import logging
import os
import sys

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────
_TRUE_VALUES = frozenset(("true", "1", "yes"))
_FALSE_VALUES = frozenset(("false", "0", "no"))

_BANNER = """
╔══════════════════════════════════════════════════════════════════╗
║          VagusPoly V2  —  PRODUCTION MODE ONLY                  ║
║  DRY_RUN concept removed. This system always runs LIVE.         ║
║  V2_MIGRATION_COMPLETE controls execution gate (not DRY_RUN).   ║
╚══════════════════════════════════════════════════════════════════╝
"""


def assert_production_mode() -> None:
    """
    V2 启动必须调用此函数。检测到任何 DRY_RUN=true 配置立即 panic 退出。

    调用位置：main.py main() 函数的最开头（setup_logger 之前）。
    """
    # 检测环境变量
    env_val = os.environ.get("DRY_RUN", "").strip().lower()
    if env_val in _TRUE_VALUES:
        _panic(
            "V2 STARTUP BLOCKED: DRY_RUN=true detected in environment variables.\n"
            "VagusPoly V2 is a production-only system. DRY_RUN mode is not supported.\n"
            "Remove DRY_RUN from your .env file and restart."
        )

    # 检测 config.json（延迟 import 避免循环依赖）
    try:
        from src.utils.config_loader import get

        cfg_dry = get("general.dry_run")
        if cfg_dry is True:
            _panic(
                "V2 STARTUP BLOCKED: config.json general.dry_run=true detected.\n"
                "VagusPoly V2 is a production-only system. DRY_RUN mode is not supported.\n"
                "Set general.dry_run=false in config.json and restart."
            )
    except Exception as _e:
        # config.json 读取失败不阻断启动（可能还未初始化）
        logger.debug(f"[V2Guard] config 检查跳过（尚未初始化）: {_e}")

    logger.info(_BANNER.strip())
    logger.info("[V2Guard] Production mode confirmed. DRY_RUN=false enforced.")


def assert_migration_gate_open(context: str = "") -> None:
    """
    执行层在真实下单前调用此函数。
    当 V2_MIGRATION_COMPLETE=false 时，抛出 MigrationGateClosedError 阻断真实下单。

    Args:
        context: 调用上下文描述（用于日志）
    """
    if not is_migration_complete():
        raise MigrationGateClosedError(
            f"V2 MIGRATION GATE CLOSED{': ' + context if context else ''}. "
            "Set V2_MIGRATION_COMPLETE=true in .env after completing Batch 2 "
            "(V2BuilderAdapter fully integrated into order_executor.py)."
        )


def is_migration_complete() -> bool:
    """检查 V2 迁移是否已完成（V2BuilderAdapter 完全接管执行层）。"""
    val = os.environ.get("V2_MIGRATION_COMPLETE", "false").strip().lower()
    return val in _TRUE_VALUES


def _panic(message: str) -> None:
    """输出致命错误信息并以退出码 2 终止进程。"""
    logger.critical(f"[V2Guard] FATAL: {message}")
    print(f"\n[V2 PRODUCTION GUARD] FATAL ERROR:\n{message}\n", file=sys.stderr)
    sys.exit(2)


class MigrationGateClosedError(RuntimeError):
    """V2 迁移门控未开放时抛出此异常，阻止真实 CLOB 下单。"""

    pass
