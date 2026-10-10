"""
dashboard_routes/price_monitor_routes.py — 价格监控 API 路由
T169: 分级涨跌记录可视化
"""

import logging
import asyncio

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from scripts.dashboard_routes._shared import DB_PATH, db_connect

router = APIRouter()
logger = logging.getLogger("web_dashboard")


def _get_price_monitor():
    """延迟导入 PriceMonitor 实例（避免循环依赖）。"""
    try:
        from main import _price_monitor

        return _price_monitor
    except Exception:
        return None


@router.get("/api/price_monitor/summary")
async def price_monitor_summary():
    """价格监控摘要统计。"""
    pm = _get_price_monitor()
    if pm:
        return pm.get_summary()
    # T590 — 同步 DB I/O 进线程，避免阻塞 event loop
    try:

        def _db():
            with db_connect() as conn:
                price_rows = conn.execute(
                    "SELECT COUNT(*) AS count FROM bet_price_series"
                ).fetchone()
                event_rows = conn.execute(
                    "SELECT COUNT(*) AS count FROM price_move_log"
                ).fetchone()
                return {
                    "enabled": False,
                    "active_bets_monitored": 0,
                    "memory_samples": 0,
                    "thresholds_crossed": 0,
                    "db_price_rows": price_rows["count"] if price_rows else 0,
                    "db_event_rows": event_rows["count"] if event_rows else 0,
                }

        return await asyncio.to_thread(_db)
    except Exception as e:
        logger.exception("[price_monitor.summary] db error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/price_monitor/events")
async def price_monitor_events(
    bet_id: str = Query(default=None, description="过滤特定bet"),
    limit: int = Query(default=200, ge=1, le=1000),
):
    """查询阈值穿越事件列表。"""
    try:

        def _db():
            with db_connect() as conn:
                if bet_id:
                    rows = conn.execute(
                        "SELECT bet_id, threshold_pct, direction, price_at_cross, "
                        "entry_price, return_pct, ts, action, category, question "
                        "FROM price_move_log WHERE bet_id = ? ORDER BY ts DESC LIMIT ?",
                        (bet_id, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT bet_id, threshold_pct, direction, price_at_cross, "
                        "entry_price, return_pct, ts, action, category, question "
                        "FROM price_move_log ORDER BY ts DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
            return [
                {
                    "bet_id": r["bet_id"],
                    "threshold_pct": r["threshold_pct"],
                    "direction": r["direction"],
                    "price_at_cross": r["price_at_cross"],
                    "entry_price": r["entry_price"],
                    "return_pct": r["return_pct"],
                    "ts": r["ts"],
                    "action": r["action"],
                    "category": r["category"],
                    "question": r["question"],
                }
                for r in rows
            ]

        return await asyncio.to_thread(_db)
    except Exception as e:
        logger.exception("[price_monitor.events] db error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/price_monitor/series/{bet_id}")
async def price_monitor_series(
    bet_id: str,
    hours: int = Query(default=24, ge=1, le=48),
):
    """查询指定 bet 的价格时序数据。"""
    from datetime import datetime, timedelta, timezone

    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    try:

        def _db():
            with db_connect() as conn:
                rows = conn.execute(
                    "SELECT ts, price_mid, price_bid, price_ask "
                    "FROM bet_price_series "
                    "WHERE bet_id = ? AND ts >= ? ORDER BY ts",
                    (bet_id, since),
                ).fetchall()

            events = []
            entry_price = None
            market_end_date = None
            outcome = None
            with db_connect() as conn:
                bet_row = conn.execute(
                    "SELECT market_price, question, category, market_end_date, outcome "
                    "FROM paper_bets WHERE bet_id = ? LIMIT 1",
                    (bet_id,),
                ).fetchone()
                if bet_row:
                    entry_price = bet_row["market_price"]
                    market_end_date = bet_row["market_end_date"]
                    outcome = bet_row["outcome"]

                evt_rows = conn.execute(
                    "SELECT threshold_pct, direction, price_at_cross, return_pct, ts "
                    "FROM price_move_log WHERE bet_id = ? ORDER BY ts",
                    (bet_id,),
                ).fetchall()
                events = [
                    {
                        "threshold_pct": e["threshold_pct"],
                        "direction": e["direction"],
                        "price_at_cross": e["price_at_cross"],
                        "return_pct": e["return_pct"],
                        "ts": e["ts"],
                    }
                    for e in evt_rows
                ]

            return {
                "bet_id": bet_id,
                "entry_price": entry_price,
                "question": bet_row["question"] if bet_row else "",
                "category": bet_row["category"] if bet_row else "",
                "market_end_date": market_end_date,
                "outcome": outcome,
                "series": [
                    {
                        "ts": r["ts"],
                        "mid": r["price_mid"],
                        "bid": r["price_bid"],
                        "ask": r["price_ask"],
                    }
                    for r in rows
                ],
                "events": events,
            }

        return await asyncio.to_thread(_db)
    except Exception as e:
        logger.exception("[price_monitor.series] db error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/price_monitor/realtime/{bet_id}")
async def price_monitor_realtime(bet_id: str):
    """获取内存中的实时10秒级数据（YES token 价格）。"""
    pm = _get_price_monitor()
    if not pm:
        return {"bet_id": bet_id, "data": [], "source": "unavailable", "outcome": None}
    data = pm.get_realtime_buffer(bet_id)
    # 查询方向，供前端 NO 转换
    outcome = None
    try:

        def _db():
            with db_connect() as conn:
                row = conn.execute(
                    "SELECT outcome FROM paper_bets WHERE bet_id = ? LIMIT 1",
                    (bet_id,),
                ).fetchone()
                return row["outcome"] if row else None

        outcome = await asyncio.to_thread(_db)
    except Exception as e:
        logger.warning(
            "[price_monitor.realtime] db lookup failed bet_id=%s err=%s", bet_id, e
        )
    return {"bet_id": bet_id, "data": data, "source": "memory", "outcome": outcome}
