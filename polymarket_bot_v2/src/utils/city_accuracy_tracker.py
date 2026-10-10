"""M5-T251: 城市历史准确率追踪模块（动态城市准确率系统）。

按城市统计过去 N 天天气策略的历史胜率，供 weather_strategy 动态调整
入场门槛和仓位大小。使用 1 小时 TTL 缓存避免频繁 DB 查询。
"""

import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── 模块级 TTL 缓存 ──────────────────────────────────────────────────────────
_CACHE: dict = {}  # {"data": {...}, "ts": float, "window_days": int}
_CACHE_TTL_SECONDS = 3600  # 1 小时


def get_city_accuracy_stats(
    window_days: int = 30,
    min_sample: int = 5,
    db_path: Optional[Path] = None,
) -> dict:
    """返回过去 window_days 天每城市的天气策略准确率统计。

    Args:
        window_days: 统计时间窗口（天），默认 30
        min_sample: 最低样本数，低于此值返回的城市 accuracy 为 None
        db_path: 数据库路径，None=默认

    Returns:
        dict[city_name, dict] 形如：
        {
            "Miami": {"total": 12, "wins": 7, "accuracy": 0.583, "pnl": 1.23},
            "London": {"total": 3, "wins": 1, "accuracy": None, "pnl": -0.5},
            ...
        }
        accuracy=None 表示样本不足 min_sample，不应触发惩罚。
    """
    global _CACHE
    now = time.monotonic()

    # 缓存命中检查（窗口一致 + TTL 内）
    if (
        _CACHE
        and _CACHE.get("window_days") == window_days
        and (now - _CACHE.get("ts", 0)) < _CACHE_TTL_SECONDS
    ):
        return _CACHE["data"]

    try:
        from src.utils.db import query_city_no_performance_windowed  # 避免循环导入

        result = query_city_no_performance_windowed(
            db_path=db_path, window_days=window_days
        )
        by_city = result.get("by_city", {})
    except Exception as exc:
        logger.warning("[CityAccuracy] DB 查询失败，返回空统计: %s", exc)
        return {}

    # 构造输出字典，处理样本不足
    output: dict = {}
    for city, stats in by_city.items():
        if not city:
            continue
        total = stats.get("total", 0)
        wins = stats.get("wins", 0)
        pnl = stats.get("pnl", 0.0)
        accuracy: Optional[float] = None
        if total >= min_sample:
            accuracy = round(wins / total, 4)
        output[city] = {
            "total": total,
            "wins": wins,
            "accuracy": accuracy,
            "pnl": pnl,
        }

    _CACHE = {"data": output, "ts": now, "window_days": window_days}
    logger.debug("[CityAccuracy] 缓存已刷新，共 %d 个城市统计", len(output))
    return output


def invalidate_cache() -> None:
    """强制清除 TTL 缓存（测试专用）。"""
    global _CACHE
    _CACHE = {}
