"""
logger.py — 统一日志配置
同时输出到控制台（彩色）和滚动文件
"""

import logging
import logging.handlers
from pathlib import Path

_INITIALIZED = False


def setup_logger(name: str = "polymarket_bot", level: str = "INFO") -> logging.Logger:
    global _INITIALIZED
    log_dir = Path(__file__).parent.parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    _level = getattr(logging, level.upper(), logging.INFO)
    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 配置根日志器，使所有模块的 getLogger(__name__) 都能正确路由
    root = logging.getLogger()
    root.setLevel(_level)

    if not _INITIALIZED:
        # 清除所有既有 handler 防止重复（例如 pytest 重新导入）
        root.handlers.clear()

        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)

        fh = logging.handlers.TimedRotatingFileHandler(
            log_dir / "bot.log",
            when="midnight",
            backupCount=30,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

        _INITIALIZED = True
    else:
        # 后续调用仅更新 level 和 format
        for h in root.handlers:
            h.setFormatter(fmt)
            h.setLevel(_level)

    # 同时返回具名 logger（兼容旧调用方式）
    log = logging.getLogger(name)
    log.setLevel(_level)
    return log


# 使应用层可直接 from src.utils.logger import log
log = setup_logger()
