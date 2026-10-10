"""
config_loader.py — 配置热加载与备份工具
支持运行时重新加载 config.json，无需重启进程
"""

import json
import logging
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).parent.parent.parent / "config" / "config.json"
_BACKUP_DIR = Path(__file__).parent.parent.parent / "config" / "backups"
_config_cache: dict = {}
_last_mtime: float = 0.0
_lock = threading.Lock()


def set_config_path(path: Path) -> None:
    """覆盖默认配置文件路径（必须在 load_config() 之前调用）。"""
    global _CONFIG_PATH, _BACKUP_DIR, _config_cache, _last_mtime
    _CONFIG_PATH = Path(path)
    _BACKUP_DIR = _CONFIG_PATH.parent / "backups"
    _config_cache = {}
    _last_mtime = 0.0
    logger.info(f"配置路径已覆盖: {_CONFIG_PATH}")


def load_config(force: bool = False) -> dict:
    """读取配置，文件修改时自动热加载。线程安全。"""
    global _config_cache, _last_mtime
    with _lock:
        try:
            mtime = _CONFIG_PATH.stat().st_mtime
            if force or mtime != _last_mtime:
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    _config_cache = json.load(f)
                _last_mtime = mtime
                logger.info(f"配置已(重新)加载: {_CONFIG_PATH}")
        except Exception as e:
            logger.error(f"加载配置失败: {e}")
            if not _config_cache:
                raise
    return _config_cache


def backup_config() -> str:
    """备份当前 config.json，返回备份路径。"""
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = _BACKUP_DIR / f"config_{ts}.json"
    shutil.copy2(_CONFIG_PATH, backup_path)
    logger.info(f"配置已备份至: {backup_path}")
    return str(backup_path)


def get(key_path: str, default=None):
    """
    点分隔路径取值，例如 get('risk.max_single_bet_pct')。
    """
    cfg = load_config()
    keys = key_path.split(".")
    val = cfg
    for k in keys:
        if isinstance(val, dict) and k in val:
            val = val[k]
        else:
            return default
    return val


def is_dry_run() -> bool:
    """
    【V2 生产铁律】VagusPoly V2 永远返回 False。DRY_RUN 概念在 V2 中不存在。

    V2 是 SaaS 多租户生产系统，不允许任何 DRY_RUN=true 路径。
    如果检测到 DRY_RUN=true 环境变量，进程会在启动时被 v2_production_guard 阻断。

    本函数保留签名兼容性，供调用方无需修改即可正确工作（所有 if dry_run: 分支永远不走）。
    """
    logger.debug("[is_dry_run] V2 PRODUCTION MODE — always False")
    return False
