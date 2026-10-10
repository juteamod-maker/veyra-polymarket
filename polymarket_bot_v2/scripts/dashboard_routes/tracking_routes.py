"""
tracking_routes.py — T280 追踪 TAB 后端 API
统一卖出事件（止损 + 止盈）查询，供 Dashboard 追踪页面使用。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Query
from scripts.dashboard_routes._shared import DB_PATH

router = APIRouter()


def _get_exit_events(
    days: int = 30,
    event_type: str = "",  # "stop_loss" | "profit_take" | "" = all
    category: str = "",
    trigger_type: str = "",
    limit: int = 200,
    db_path: Path | None = None,
) -> list[dict]:
    """
    合并查询止损事件（stop_loss_events 表）和止盈事件（paper_bets 表）。
    返回统一字段列表，按时间倒序。
    """
    from src.utils.database import connect as _pg_connect

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    results: list[dict] = []

    conn = _pg_connect()
    try:
        # ── 1. 止损事件（stop_loss_events 表）─────────────────────────
        if event_type in ("", "stop_loss"):
            sl_where = ["timestamp >= ?"]
            sl_params: list = [cutoff]
            if category:
                sl_where.append("category = ?")
                sl_params.append(category)
            if trigger_type:
                sl_where.append("trigger_type = ?")
                sl_params.append(trigger_type)
            sl_sql = (
                "SELECT id, timestamp, bet_id, question, category, trigger_type,"
                " entry_price, exit_price, return_pct, pnl, size_usdc,"
                " conditions_json, context_json"
                " FROM stop_loss_events"
                " WHERE id IN ("
                "   SELECT MAX(id) FROM stop_loss_events"
                "   WHERE " + " AND ".join(sl_where) + " GROUP BY bet_id, trigger_type"
                " ) ORDER BY id DESC LIMIT ?"
            )
            sl_params.append(limit)
            rows = conn.execute(sl_sql, sl_params).fetchall()
            for r in rows:
                results.append(
                    {
                        "event_type": "stop_loss",
                        "timestamp": r["timestamp"],
                        "bet_id": r["bet_id"],
                        "question": r["question"] or "",
                        "category": r["category"] or "",
                        "trigger_type": r["trigger_type"] or "standard_sl",
                        "entry_price": r["entry_price"] or 0.0,
                        "exit_price": r["exit_price"] or 0.0,
                        "return_pct": r["return_pct"] or 0.0,
                        "pnl": r["pnl"] or 0.0,
                        "size_usdc": r["size_usdc"] or 0.0,
                        "policy_trace": r["trigger_type"] or "standard_sl",
                        "conditions": r["conditions_json"] or "{}",
                        "context": r["context_json"] or "{}",
                    }
                )

        # ── 2. 止盈事件（paper_bets 表）──────────────────────────────
        if event_type in ("", "profit_take"):
            pt_where = [
                "exit_type = 'profit_take'",
                "resolved_at IS NOT NULL",
                "datetime(resolved_at, 'unixepoch') >= ?",
            ]
            # Convert cutoff ISO to rough unix for the datetime comparison
            cutoff_unix = (
                datetime.now(timezone.utc) - timedelta(days=days)
            ).timestamp()
            pt_params: list = [cutoff]
            # Better: compare resolved_at numerically
            pt_where = [
                "exit_type = 'profit_take'",
                "resolved_at IS NOT NULL",
                "resolved_at >= ?",
            ]
            pt_params = [cutoff_unix]
            if category:
                pt_where.append("category = ?")
                pt_params.append(category)
            # trigger_type filter for profit_take: map to strategy-level labels
            # (paper_bets doesn't store sub-trigger, skip if specific SL trigger requested)
            if trigger_type and trigger_type not in (
                "ceiling",
                "trailing",
                "expiry",
                "quick_exit",
                "weather_quick",
                "standard_profit_take",
                "floor_protection",
                "certainty_sell",
                "live_ceiling",
                "live_standard",
                "weather_rotation_exit",
                "legacy_cleanup",
                "profit_take",
            ):
                # filter is for a SL trigger type — no profit_take rows match
                pt_params = []
            if pt_params:
                pt_sql = (
                    "SELECT bet_id, question, category, market_price, exit_price,"
                    " pnl, size_usdc, resolved_at"
                    " FROM paper_bets"
                    " WHERE "
                    + " AND ".join(pt_where)
                    + " ORDER BY resolved_at DESC LIMIT ?"
                )
                pt_params.append(limit)
                rows = conn.execute(pt_sql, pt_params).fetchall()
                for r in rows:
                    entry = float(r["market_price"] or 0)
                    ext = float(r["exit_price"] or 0)
                    ret = (ext - entry) / entry if entry > 0 else 0.0
                    # Convert resolved_at unix timestamp to ISO string
                    try:
                        ts = datetime.fromtimestamp(
                            float(r["resolved_at"]), tz=timezone.utc
                        ).isoformat()
                    except Exception:
                        ts = ""
                    results.append(
                        {
                            "event_type": "profit_take",
                            "timestamp": ts,
                            "bet_id": r["bet_id"],
                            "question": r["question"] or "",
                            "category": r["category"] or "",
                            "trigger_type": "profit_take",
                            "entry_price": entry,
                            "exit_price": ext,
                            "return_pct": ret,
                            "pnl": float(r["pnl"] or 0),
                            "size_usdc": float(r["size_usdc"] or 0),
                            "policy_trace": "profit_take",
                            "conditions": "{}",
                            "context": "{}",
                        }
                    )
    finally:
        conn.close()

    # Sort merged list by timestamp descending
    results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return results[:limit]


def _get_tracking_summary(days: int = 30, db_path: Path | None = None) -> dict:
    """生成追踪统计摘要：总计、按类型、按赛道、时间序列。"""
    from src.utils.database import connect as _pg_connect

    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff_dt.isoformat()
    cutoff_unix = cutoff_dt.timestamp()

    conn = _pg_connect()
    try:
        # ── 止损汇总（T195: 使用子查询去重，每bet+trigger仅计一次）──
        _sl_dedup_sub = (
            "SELECT MAX(id) as id FROM stop_loss_events"
            " WHERE timestamp >= ? GROUP BY bet_id, trigger_type"
        )
        sl_total = conn.execute(
            "SELECT COUNT(*) as cnt, ROUND(SUM(COALESCE(e.pnl,0)),4) as total_pnl"
            " FROM stop_loss_events e"
            " INNER JOIN (" + _sl_dedup_sub + ") d ON e.id = d.id",
            (cutoff_iso,),
        ).fetchone()

        sl_by_trigger = conn.execute(
            "SELECT e.trigger_type, COUNT(*) as cnt,"
            " ROUND(AVG(e.return_pct),4) as avg_return,"
            " ROUND(SUM(COALESCE(e.pnl,0)),4) as total_pnl"
            " FROM stop_loss_events e"
            " INNER JOIN (" + _sl_dedup_sub + ") d ON e.id = d.id"
            " GROUP BY e.trigger_type ORDER BY cnt DESC",
            (cutoff_iso,),
        ).fetchall()

        sl_by_cat = conn.execute(
            "SELECT e.category, COUNT(*) as cnt,"
            " ROUND(AVG(e.return_pct),4) as avg_return,"
            " ROUND(SUM(COALESCE(e.pnl,0)),4) as total_pnl"
            " FROM stop_loss_events e"
            " INNER JOIN (" + _sl_dedup_sub + ") d ON e.id = d.id"
            " GROUP BY e.category ORDER BY cnt DESC",
            (cutoff_iso,),
        ).fetchall()

        # Daily SL time series
        sl_daily = conn.execute(
            "SELECT substr(e.timestamp,1,10) as day, COUNT(*) as cnt,"
            " ROUND(SUM(COALESCE(e.pnl,0)),4) as pnl"
            " FROM stop_loss_events e"
            " INNER JOIN (" + _sl_dedup_sub + ") d ON e.id = d.id"
            " GROUP BY day ORDER BY day",
            (cutoff_iso,),
        ).fetchall()

        # ── 止盈汇总 ─────────────────────────────────────────────────
        pt_total = conn.execute(
            "SELECT COUNT(*) as cnt, ROUND(SUM(COALESCE(pnl,0)),4) as total_pnl"
            " FROM paper_bets"
            " WHERE exit_type='profit_take' AND resolved_at IS NOT NULL AND resolved_at >= ?",
            (cutoff_unix,),
        ).fetchone()

        pt_by_cat = conn.execute(
            "SELECT category, COUNT(*) as cnt,"
            " ROUND(AVG(CASE WHEN market_price > 0 THEN (exit_price-market_price)/market_price ELSE 0 END),4) as avg_return,"
            " ROUND(SUM(COALESCE(pnl,0)),4) as total_pnl"
            " FROM paper_bets"
            " WHERE exit_type='profit_take' AND resolved_at IS NOT NULL AND resolved_at >= ?"
            " GROUP BY category ORDER BY cnt DESC",
            (cutoff_unix,),
        ).fetchall()

        # Daily PT time series
        pt_daily = conn.execute(
            "SELECT date(resolved_at,'unixepoch') as day, COUNT(*) as cnt,"
            " ROUND(SUM(COALESCE(pnl,0)),4) as pnl"
            " FROM paper_bets"
            " WHERE exit_type='profit_take' AND resolved_at IS NOT NULL AND resolved_at >= ?"
            " GROUP BY day ORDER BY day",
            (cutoff_unix,),
        ).fetchall()

    finally:
        conn.close()

    # ── 合并时间序列到统一日历 ───────────────────────────────────────
    sl_daily_map = {r["day"]: {"cnt": r["cnt"], "pnl": r["pnl"]} for r in sl_daily}
    pt_daily_map = {r["day"]: {"cnt": r["cnt"], "pnl": r["pnl"]} for r in pt_daily}
    all_days = sorted(set(sl_daily_map.keys()) | set(pt_daily_map.keys()))
    time_series = [
        {
            "day": d,
            "sl_cnt": sl_daily_map.get(d, {}).get("cnt", 0),
            "sl_pnl": sl_daily_map.get(d, {}).get("pnl", 0),
            "pt_cnt": pt_daily_map.get(d, {}).get("cnt", 0),
            "pt_pnl": pt_daily_map.get(d, {}).get("pnl", 0),
        }
        for d in all_days
    ]

    sl_cnt = sl_total["cnt"] if sl_total else 0
    sl_pnl = float(sl_total["total_pnl"] or 0) if sl_total else 0.0
    pt_cnt = pt_total["cnt"] if pt_total else 0
    pt_pnl = float(pt_total["total_pnl"] or 0) if pt_total else 0.0

    return {
        "days": days,
        "stop_loss": {
            "total": sl_cnt,
            "total_pnl": sl_pnl,
            "by_trigger": [dict(r) for r in sl_by_trigger],
            "by_category": [dict(r) for r in sl_by_cat],
        },
        "profit_take": {
            "total": pt_cnt,
            "total_pnl": pt_pnl,
            "by_category": [dict(r) for r in pt_by_cat],
        },
        "combined": {
            "total": sl_cnt + pt_cnt,
            "total_pnl": round(sl_pnl + pt_pnl, 4),
            "win_rate": (
                round(pt_cnt / (sl_cnt + pt_cnt), 4) if (sl_cnt + pt_cnt) > 0 else 0
            ),
        },
        "time_series": time_series,
    }


# ── 路由 ──────────────────────────────────────────────────────────────


@router.get("/api/tracking/events")
async def api_tracking_events(
    days: int = Query(30, ge=1, le=365),
    event_type: str = Query("", description="stop_loss | profit_take | (empty=all)"),
    category: str = Query(""),
    trigger_type: str = Query(""),
    limit: int = Query(100, ge=1, le=500),
):
    """统一查询止损 + 止盈卖出事件列表。"""
    events = _get_exit_events(
        days=days,
        event_type=event_type,
        category=category,
        trigger_type=trigger_type,
        limit=limit,
    )
    return {"events": events, "total": len(events)}


@router.get("/api/tracking/summary")
async def api_tracking_summary(days: int = Query(30, ge=1, le=365)):
    """追踪统计摘要：止损 + 止盈总计、按赛道、按触发类型、时间序列。"""
    return _get_tracking_summary(days=days)
