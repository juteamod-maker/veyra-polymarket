"""
backtest_engine.py — T240 多赛道统一回测引擎

按时间窗口从 DB 查询投注记录，计算全局/赛道/止损/信号质量统计。
支持 fast 模式（纯DB，<1s）和 deep 模式（含 Polymarket API 止损分类）。

用法：
    from src.backtest.backtest_engine import run_backtest
    result = run_backtest(mode="days", value=7)
    result = run_backtest(mode="hours", value=24)
    result = run_backtest(mode="range", start="2026-03-01", end="2026-03-28")
"""

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 赛道图标
TRACK_ICONS = {
    "weather": "🌤",
    "sports": "⚽",
    "crypto": "₿",
    "geopolitics": "🌍",
    "finance": "💰",
    "pop_culture": "🎭",
    "tech": "📊",
}


def run_backtest(
    mode: str = "days",
    value: int = 7,
    start: str = "",
    end: str = "",
    track: str = "",
    deep: bool = False,
    db_path: Path | None = None,
) -> dict:
    """
    运行回测分析。

    Args:
        mode: "hours" | "days" | "range"
        value: 当 mode="hours" 或 "days" 时的数值
        start: 当 mode="range" 时的开始日期 (YYYY-MM-DD)
        end: 当 mode="range" 时的结束日期 (YYYY-MM-DD)
        track: 赛道过滤（如 "weather", "sports"），空字符串表示全部
        deep: 是否启用深度止损分类（需要 Polymarket API）
        db_path: 数据库路径（测试用）

    Returns:
        包含回测结果的字典
    """
    t0 = time.monotonic()
    now = datetime.now(timezone.utc)

    # ── 计算时间范围 ──────────────────────────────────────────
    if mode == "hours":
        start_dt = now - timedelta(hours=max(value, 1))
        end_dt = now
    elif mode == "days":
        start_dt = now - timedelta(days=max(value, 1))
        end_dt = now
    elif mode == "range":
        start_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
        end_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
        if end_dt <= start_dt:
            start_dt, end_dt = end_dt, start_dt
    else:
        start_dt = now - timedelta(days=7)
        end_dt = now

    start_ts = start_dt.timestamp()
    end_ts = end_dt.timestamp()

    # ── 查询数据 ─────────────────────────────────────────────
    bets = _query_bets(start_ts, end_ts, db_path)

    # ── 赛道过滤 ─────────────────────────────────────────────
    if track:
        bets = [b for b in bets if (b.get("category") or "").lower() == track.lower()]

    if not bets:
        return {
            "query": {
                "mode": mode,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "total_bets": 0,
            },
            "global": _empty_global(),
            "by_category": {},
            "stop_quality": _empty_stop_quality(),
            "signal_quality": _empty_signal_quality(),
            "elapsed_ms": round((time.monotonic() - t0) * 1000),
        }

    # ── 分类投注 ─────────────────────────────────────────────
    classified = _classify_bets(bets)

    # ── 全局统计 ─────────────────────────────────────────────
    global_stats = _compute_global_stats(bets, classified)

    # ── 赛道统计 ─────────────────────────────────────────────
    by_category = _compute_category_stats(bets, classified)

    # ── 止损质量 ─────────────────────────────────────────────
    stop_quality = _compute_stop_quality(classified, deep=deep, db_path=db_path)

    # ── 信号质量 ─────────────────────────────────────────────
    signal_quality = _compute_signal_quality(bets)

    elapsed = round((time.monotonic() - t0) * 1000)

    return {
        "query": {
            "mode": mode,
            "start": start_dt.isoformat(),
            "end": end_dt.isoformat(),
            "total_bets": len(bets),
        },
        "global": global_stats,
        "by_category": by_category,
        "stop_quality": stop_quality,
        "signal_quality": signal_quality,
        "elapsed_ms": elapsed,
    }


# ── 内部实现 ─────────────────────────────────────────────────


def _query_bets(start_ts: float, end_ts: float, db_path=None) -> list[dict]:
    """从 DB 查询时间范围内的所有投注。"""
    from src.utils.db import connect

    with connect(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM paper_bets
               WHERE ts_entered >= ? AND ts_entered <= ?
               ORDER BY ts_entered DESC""",
            (start_ts, end_ts),
        ).fetchall()
    return [dict(r) for r in rows]


def _classify_bets(bets: list[dict]) -> dict:
    """将投注按退出类型分类。"""
    result = {
        "open": [],
        "resolution": [],
        "stop_loss": [],
        "profit_take": [],
        "manual_exit": [],
        "wu_temp_breach": [],
        "other": [],
    }
    for bet in bets:
        exit_type = (bet.get("exit_type") or "").lower()
        if not exit_type or exit_type == "":
            result["open"].append(bet)
        elif exit_type == "resolution":
            result["resolution"].append(bet)
        elif exit_type == "stop_loss":
            result["stop_loss"].append(bet)
        elif exit_type == "profit_take":
            result["profit_take"].append(bet)
        elif exit_type in ("manual_exit", "manual"):
            result["manual_exit"].append(bet)
        elif exit_type == "wu_temp_breach":
            result["wu_temp_breach"].append(bet)
        else:
            result["other"].append(bet)
    return result


def _compute_global_stats(bets: list[dict], classified: dict) -> dict:
    """计算全局统计。"""
    total = len(bets)
    closed = [b for b in bets if b.get("exit_type")]
    wins = [b for b in closed if b.get("is_win") == 1]
    losses = [b for b in closed if b.get("is_win") == 0]

    pnls = [float(b.get("pnl") or 0) for b in closed if b.get("pnl") is not None]
    total_pnl = round(sum(pnls), 4) if pnls else 0
    total_cost = sum(float(b.get("size_usdc") or 0) for b in closed)
    roi = round(total_pnl / total_cost * 100, 2) if total_cost > 0 else 0

    return {
        "total": total,
        "open": len(classified["open"]),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_pnl": total_pnl,
        "roi_pct": roi,
        "avg_pnl": round(total_pnl / len(closed), 4) if closed else 0,
        "max_win": round(max(pnls), 4) if pnls else 0,
        "max_loss": round(min(pnls), 4) if pnls else 0,
        "total_cost": round(total_cost, 2),
        "by_exit_type": {k: len(v) for k, v in classified.items()},
    }


def _compute_category_stats(bets: list[dict], classified: dict) -> dict:
    """按赛道计算统计。"""
    cats: dict[str, list[dict]] = {}
    for bet in bets:
        cat = (bet.get("category") or "other").lower()
        cats.setdefault(cat, []).append(bet)

    result = {}
    for cat, cat_bets in sorted(cats.items()):
        closed = [b for b in cat_bets if b.get("exit_type")]
        wins = [b for b in closed if b.get("is_win") == 1]
        pnls = [float(b.get("pnl") or 0) for b in closed if b.get("pnl") is not None]
        total_pnl = round(sum(pnls), 4) if pnls else 0
        total_cost = sum(float(b.get("size_usdc") or 0) for b in closed)

        stops = [b for b in cat_bets if (b.get("exit_type") or "") == "stop_loss"]
        profits = [b for b in cat_bets if (b.get("exit_type") or "") == "profit_take"]

        result[cat] = {
            "icon": TRACK_ICONS.get(cat, "📦"),
            "total": len(cat_bets),
            "closed": len(closed),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0,
            "total_pnl": total_pnl,
            "roi_pct": round(total_pnl / total_cost * 100, 2) if total_cost > 0 else 0,
            "avg_pnl": round(total_pnl / len(closed), 4) if closed else 0,
            "stops": len(stops),
            "profit_takes": len(profits),
        }

    return result


def _compute_stop_quality(classified: dict, deep: bool = False, db_path=None) -> dict:
    """计算止损质量分析。

    fast 模式: 仅从 DB 统计止损数量和 PnL
    deep 模式: 通过 Polymarket API 查询实际结算结果，做 correct/wrong 分类
    """
    stops = classified.get("stop_loss", [])
    if not stops:
        return _empty_stop_quality()

    stop_pnls = [float(b.get("pnl") or 0) for b in stops]

    # 按赛道统计
    by_cat: dict[str, list] = {}
    for b in stops:
        cat = (b.get("category") or "other").lower()
        by_cat.setdefault(cat, []).append(b)

    by_category = []
    for cat, cat_stops in sorted(by_cat.items()):
        cat_pnls = [float(b.get("pnl") or 0) for b in cat_stops]
        by_category.append(
            {
                "category": cat,
                "icon": TRACK_ICONS.get(cat, "📦"),
                "count": len(cat_stops),
                "total_pnl": round(sum(cat_pnls), 4),
                "avg_pnl": round(sum(cat_pnls) / len(cat_pnls), 4) if cat_pnls else 0,
            }
        )

    # 按 bet_bucket 统计
    by_bucket: dict[str, int] = {}
    for b in stops:
        bucket = (b.get("bet_bucket") or "standard").lower()
        by_bucket[bucket] = by_bucket.get(bucket, 0) + 1

    result = {
        "total_stops": len(stops),
        "total_pnl": round(sum(stop_pnls), 4),
        "avg_pnl": round(sum(stop_pnls) / len(stop_pnls), 4) if stop_pnls else 0,
        "max_loss": round(min(stop_pnls), 4) if stop_pnls else 0,
        "by_category": by_category,
        "by_bucket": by_bucket,
        "deep_analysis": None,
    }

    # Deep mode: query Polymarket API for actual outcomes
    if deep and stops:
        result["deep_analysis"] = _deep_stop_classification(stops)

    return result


def _deep_stop_classification(stops: list[dict]) -> dict:
    """深度止损分类：查询 Polymarket API 获取实际市场结算结果。

    返回 correct/wrong/premature/pending 分类。
    """
    import requests

    # 获取唯一 market_ids
    market_ids = list({b.get("market_id", "") for b in stops if b.get("market_id")})
    _slug_map = {
        b.get("market_id", ""): b.get("market_slug", "")
        for b in stops
        if b.get("market_slug")
    }
    resolutions: dict[str, str | None] = {}  # market_id → "YES" | "NO" | None

    # 批量查询（5 个一批）
    for i in range(0, len(market_ids), 5):
        batch = market_ids[i : i + 5]
        for mid in batch:
            try:
                if mid.startswith("0x"):
                    _bt_slug = _slug_map.get(mid, "")
                    _q_params = (
                        {"slug": _bt_slug} if _bt_slug else {"condition_id": mid}
                    )
                    resp = requests.get(
                        "https://gamma-api.polymarket.com/markets",
                        params=_q_params,
                        timeout=5,
                    )
                else:
                    resp = requests.get(
                        f"https://gamma-api.polymarket.com/markets/{mid}",
                        timeout=5,
                    )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list):
                        data = data[0] if data else {}
                    prices_raw = data.get("outcomePrices", "")
                    if isinstance(prices_raw, str):
                        prices = json.loads(prices_raw)
                    elif isinstance(prices_raw, list):
                        prices = prices_raw
                    else:
                        prices = []

                    if len(prices) >= 2:
                        yes_p = float(prices[0])
                        no_p = float(prices[1])
                        if yes_p >= 0.95:
                            resolutions[mid] = "YES"
                        elif no_p >= 0.95:
                            resolutions[mid] = "NO"
                        else:
                            resolutions[mid] = None  # not yet resolved
                    else:
                        resolutions[mid] = None
                else:
                    resolutions[mid] = None
            except Exception:
                resolutions[mid] = None

    # 分类
    correct = []
    wrong = []
    pending = []

    for bet in stops:
        mid = bet.get("market_id", "")
        outcome = (bet.get("outcome") or "").strip().upper()
        winner = resolutions.get(mid)

        if winner is None:
            pending.append(_stop_summary(bet, "pending"))
            continue

        if outcome == winner:
            # 我们投的方向最终赢了 → 错误止损
            wrong.append(_stop_summary(bet, "wrong"))
        else:
            # 我们投的方向最终输了 → 正确止损
            correct.append(_stop_summary(bet, "correct"))

    return {
        "correct": len(correct),
        "wrong": len(wrong),
        "pending": len(pending),
        "correct_pnl": round(sum(s["pnl"] for s in correct), 4),
        "wrong_pnl": round(sum(s["pnl"] for s in wrong), 4),
        "details": {
            "correct": correct[:10],  # 限制返回前10条
            "wrong": wrong[:10],
            "pending": pending[:5],
        },
    }


def _stop_summary(bet: dict, classification: str) -> dict:
    """为止损投注生成摘要。"""
    return {
        "bet_id": bet.get("bet_id", "")[:12],
        "question": (bet.get("question") or "")[:60],
        "category": bet.get("category", ""),
        "outcome": bet.get("outcome", ""),
        "entry_price": float(bet.get("market_price") or 0),
        "exit_price": float(bet.get("exit_price") or 0),
        "pnl": float(bet.get("pnl") or 0),
        "classification": classification,
    }


def _compute_signal_quality(bets: list[dict]) -> dict:
    """计算AI信号质量分析。"""
    closed = [b for b in bets if b.get("exit_type")]
    if not closed:
        return _empty_signal_quality()

    # ── EV 分带分析 ──────────────────────────────────────────
    ev_bands = [
        {"label": "0-5%", "min": 0, "max": 0.05},
        {"label": "5-10%", "min": 0.05, "max": 0.10},
        {"label": "10-15%", "min": 0.10, "max": 0.15},
        {"label": "15-20%", "min": 0.15, "max": 0.20},
        {"label": "20%+", "min": 0.20, "max": 999},
    ]

    by_ev = []
    for band in ev_bands:
        in_band = [
            b for b in closed if band["min"] <= float(b.get("ev") or 0) < band["max"]
        ]
        if not in_band:
            continue
        wins = [b for b in in_band if b.get("is_win") == 1]
        pnls = [float(b.get("pnl") or 0) for b in in_band]
        by_ev.append(
            {
                "range": band["label"],
                "count": len(in_band),
                "win_rate": round(len(wins) / len(in_band) * 100, 1),
                "avg_pnl": round(sum(pnls) / len(pnls), 4),
                "total_pnl": round(sum(pnls), 4),
            }
        )

    # ── 置信度分带分析 ──────────────────────────────────────
    conf_bands = [
        {"label": "0.5-0.6", "min": 0.5, "max": 0.6},
        {"label": "0.6-0.7", "min": 0.6, "max": 0.7},
        {"label": "0.7-0.8", "min": 0.7, "max": 0.8},
        {"label": "0.8-0.9", "min": 0.8, "max": 0.9},
        {"label": "0.9-1.0", "min": 0.9, "max": 1.01},
    ]

    by_confidence = []
    for band in conf_bands:
        in_band = [
            b
            for b in closed
            if band["min"] <= float(b.get("confidence") or 0) < band["max"]
        ]
        if not in_band:
            continue
        wins = [b for b in in_band if b.get("is_win") == 1]
        pnls = [float(b.get("pnl") or 0) for b in in_band]
        by_confidence.append(
            {
                "range": band["label"],
                "count": len(in_band),
                "win_rate": round(len(wins) / len(in_band) * 100, 1),
                "avg_pnl": round(sum(pnls) / len(pnls), 4),
            }
        )

    # ── AI 模型分析 ─────────────────────────────────────────
    model_stats: dict[str, dict] = {}
    for b in closed:
        models_str = b.get("ai_models") or ""
        if not models_str:
            continue
        # models 字段格式: "model1,model2,model3" 或 JSON array
        try:
            models = (
                json.loads(models_str)
                if models_str.startswith("[")
                else models_str.split(",")
            )
        except (json.JSONDecodeError, AttributeError):
            models = [models_str]

        for model in models:
            model = model.strip()
            if not model:
                continue
            if model not in model_stats:
                model_stats[model] = {"wins": 0, "total": 0, "pnl": 0}
            model_stats[model]["total"] += 1
            if b.get("is_win") == 1:
                model_stats[model]["wins"] += 1
            model_stats[model]["pnl"] += float(b.get("pnl") or 0)

    by_model = []
    for name, stats in sorted(model_stats.items(), key=lambda x: -x[1]["total"]):
        by_model.append(
            {
                "name": name[:40],
                "count": stats["total"],
                "win_rate": (
                    round(stats["wins"] / stats["total"] * 100, 1)
                    if stats["total"]
                    else 0
                ),
                "total_pnl": round(stats["pnl"], 4),
            }
        )

    # ── Bucket 分析（Scanner 质量）─────────────────────────
    bucket_stats: dict[str, dict] = {}
    for b in closed:
        bucket = (b.get("bet_bucket") or "standard").lower()
        if bucket not in bucket_stats:
            bucket_stats[bucket] = {"wins": 0, "total": 0, "pnl": 0}
        bucket_stats[bucket]["total"] += 1
        if b.get("is_win") == 1:
            bucket_stats[bucket]["wins"] += 1
        bucket_stats[bucket]["pnl"] += float(b.get("pnl") or 0)

    by_bucket = []
    for name, stats in sorted(bucket_stats.items(), key=lambda x: -x[1]["total"]):
        by_bucket.append(
            {
                "bucket": name,
                "count": stats["total"],
                "win_rate": (
                    round(stats["wins"] / stats["total"] * 100, 1)
                    if stats["total"]
                    else 0
                ),
                "total_pnl": round(stats["pnl"], 4),
            }
        )

    return {
        "by_ev": by_ev,
        "by_confidence": by_confidence,
        "by_model": by_model[:10],  # Top 10 models
        "by_bucket": by_bucket,
    }


# ── 空结构 ───────────────────────────────────────────────────


def _empty_global() -> dict:
    return {
        "total": 0,
        "open": 0,
        "closed": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0,
        "total_pnl": 0,
        "roi_pct": 0,
        "avg_pnl": 0,
        "max_win": 0,
        "max_loss": 0,
        "total_cost": 0,
        "by_exit_type": {},
    }


def _empty_stop_quality() -> dict:
    return {
        "total_stops": 0,
        "total_pnl": 0,
        "avg_pnl": 0,
        "max_loss": 0,
        "by_category": [],
        "by_bucket": {},
        "deep_analysis": None,
    }


def _empty_signal_quality() -> dict:
    return {
        "by_ev": [],
        "by_confidence": [],
        "by_model": [],
        "by_bucket": [],
    }
