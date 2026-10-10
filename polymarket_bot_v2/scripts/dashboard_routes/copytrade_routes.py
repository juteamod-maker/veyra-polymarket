"""
copytrade_routes.py — T298 跟投 TAB 后端 API
PolyHub Discover 代理 + 本地跟投任务 CRUD
"""

from __future__ import annotations

import asyncio  # T590 wave 4
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from scripts.dashboard_routes._shared import (
    DB_PATH,
    db_connect,
    get_request_identity,
    logger,
)
from src.utils.config_loader import get

router = APIRouter()

# ── PolyHub API helpers ───────────────────────────────────────────────


def _polyhub_base() -> str:
    return str(
        get("polyhub_discover.api_base")
        or "https://polyhub.skill-test.bedev.hubble-rpc.xyz"
    )


def _polyhub_timeout() -> int:
    return int(get("polyhub_discover.request_timeout_sec") or 15)


def _normalize_trader(t: dict, query_tag: str = "") -> dict:
    """标准化 PolyHub camelCase 字段为前端期望的 snake_case 字段。
    query_tag: 本次查询使用的标签，用于覆盖 PolyHub 返回的 tag（PolyHub 历史分类可能不准）"""
    out = dict(t)
    # 地址统一到 user_id
    out["user_id"] = str(t.get("user_id") or t.get("user") or t.get("address") or "")
    out["address"] = out["user_id"]
    # 字段别名（camelCase → snake_case）
    out["ev_per_bought"] = float(t.get("ev_per_bought") or t.get("evPerBought") or 0.0)
    out["timing_score"] = float(t.get("timing_score") or t.get("timingScore") or 50.0)
    out["trade_count_30"] = int(
        float(
            t.get("trade_count_30")
            or t.get("trade_count_30d")
            or t.get("tradeCount30")
            or 0.0
        )
    )
    out["pnl"] = float(t.get("pnl") or 0.0)
    out["is_bot"] = bool(t.get("is_bot") or t.get("isBot") or False)
    # tag: 优先使用 query_tag（查询标签），回退到 PolyHub 返回的 tag
    # 注意：PolyHub 的 tag 分类基于历史数据，可能与近期实际持仓不符
    out["tag"] = query_tag if query_tag else str(t.get("tag") or "")
    out["tag_source"] = "query" if query_tag else "polyhub"
    return out


def _compute_secondary_score(trader: dict) -> float:
    """
    二次打分公式（0-100）基于真实 PolyHub 数据分布重新校准。
    评分维度：EV优势(35) + PnL(35) + 活跃度(15) + 时机bonus(15) - Bot惩罚(15)。
    兼容 camelCase（PolyHub 原始）和 snake_case（已标准化）字段。
    """
    ev = float(trader.get("ev_per_bought") or trader.get("evPerBought") or 0.0)
    timing = float(trader.get("timing_score") or trader.get("timingScore") or 50.0)
    trades30 = float(
        trader.get("trade_count_30")
        or trader.get("trade_count_30d")
        or trader.get("tradeCount30")
        or 0.0
    )
    pnl = float(trader.get("pnl") or 0.0)
    is_bot = bool(trader.get("is_bot") or trader.get("isBot") or False)

    # EV 分（0-35）：实际数据 ev ≈ 0.01–0.1，以 0.1 为满分基准
    ev_score = min(ev / 0.1, 1.0) * 35.0

    # PnL 分（0-35）：以 $1M 为满分基准（真实头部 Trader PnL 多为百万级）
    pnl_score = min(pnl / 1_000_000.0, 1.0) * 35.0

    # 活跃度分（0-15）：30天交易 ≥ 500 次为满分
    trades_score = min(trades30 / 500.0, 1.0) * 15.0

    # 时机 bonus（0-15）：timing_score = 50.0 是大多数 Trader 的默认值，无意义；
    # 只有显著高于 50 才给分
    timing_bonus = 0.0
    if timing != 50.0 and timing > 50.0:
        timing_bonus = max(0.0, min((timing - 50.0) / 25.0, 1.0)) * 15.0

    # Bot 惩罚：-15 分（让人工 Trader 优先）
    bot_penalty = 15.0 if is_bot else 0.0

    return max(
        0.0, round(ev_score + pnl_score + trades_score + timing_bonus - bot_penalty, 2)
    )


# ── PolyHub Proxy Endpoints ───────────────────────────────────────────


@router.get("/api/polyhub/tags")
async def polyhub_tags() -> JSONResponse:
    """返回 config.json polyhub_discover.tag_focus 作为标签列表（不从PolyHub拉）"""
    tag_focus = get("polyhub_discover.tag_focus") or ["Weather", "Sports", "Crypto"]
    if not isinstance(tag_focus, list):
        tag_focus = [str(tag_focus)]
    return JSONResponse(content={"tags": tag_focus})


@router.get("/api/polyhub/config")
async def polyhub_config() -> JSONResponse:
    """返回前端筛选器所需的默认参数（从 config.json polyhub_discover 读取）"""
    return JSONResponse(
        content={
            "pnl_min": float(get("polyhub_discover.min_pnl_usd") or 1000),
            "trades_min": int(get("polyhub_discover.min_trades") or 0),
            "ev_min": float(get("polyhub_discover.min_ev_bought") or 0.005),
            "timing_min": float(get("polyhub_discover.min_timing_score") or 0),
            "min_score": float(get("polyhub_discover.min_score") or 0),
            "exclude_bots": bool(
                get("polyhub_discover.exclude_bots")
                if get("polyhub_discover.exclude_bots") is not None
                else True
            ),
            "default_time_window": str(
                get("polyhub_discover.default_time_window") or "30d"
            ),
            "max_results": int(get("polyhub_discover.max_results") or 50),
            "tag_focus": get("polyhub_discover.tag_focus")
            or ["Weather", "Sports", "Crypto"],
        }
    )


@router.get("/api/polyhub/discover")
async def polyhub_discover(
    tag: str = Query(..., description="市场标签（如 Sports/Weather/Crypto）"),
    time_range: str = Query("30d", description="时间窗口：all | 30d"),
    pnl_min: float = Query(0, description="最低PnL（USD）"),
    trade_count_30_min: int = Query(0, description="30天最低交易次数"),
    ev_per_bought_min: float = Query(0.0, description="最低EV/Bought"),
    timing_score_min: float = Query(0.0, description="最低Alpha评分"),
    filter_bots: int = Query(1, description="是否过滤bot：0=不过滤 1=过滤"),
    sort_by: str = Query("ev_per_bought", description="排序字段"),
    sort_direction: str = Query("desc", description="排序方向：asc | desc"),
    limit: int = Query(50, ge=1, le=200, description="最大结果数"),
    min_score: float = Query(0.0, description="最低二次评分（0-100）"),
    niche_only: int = Query(1, description="只看专注该标签的交易者：1=是 0=否"),
) -> JSONResponse:
    """代理 PolyHub traders-v2 并追加二次打分"""
    url = f"{_polyhub_base()}/api/v1/traders-v2/"
    params: dict[str, Any] = {
        "tag": tag,
        "time_range": time_range,
        "filterBots": filter_bots,
        "pnl_min": pnl_min,
        "trade_count_30_min": trade_count_30_min,
        "ev_per_bought_min": ev_per_bought_min,
        "timing_score_min": timing_score_min,
        "sort_by": sort_by,
        "sort_direction": sort_direction,
        "limit": limit,
        "niche_only": niche_only,  # 限定专注该标签的交易者，减少跨类别噪声
    }
    try:
        resp = requests.get(url, params=params, timeout=_polyhub_timeout())
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        logger.warning("PolyHub discover fetch failed: %s", exc)
        raise HTTPException(
            status_code=502, detail=f"PolyHub API error: {exc}"
        ) from exc

    # 兼容 {rows: [...]}, {traders: [...]}, {data: [...]}, 或直接列表
    raw_traders: list[dict] = (
        data
        if isinstance(data, list)
        else data.get("rows", data.get("traders", data.get("data", [])))
    )

    # 标准化字段名（camelCase → snake_case）并追加二次打分
    # 传入 query_tag 覆盖 PolyHub 历史分类 tag（tag分类可能与近期实际持仓不符）
    traders: list[dict] = []
    for t in raw_traders:
        normalized = _normalize_trader(t, query_tag=tag)
        normalized["secondary_score"] = _compute_secondary_score(normalized)
        traders.append(normalized)

    if min_score > 0:
        traders = [t for t in traders if t["secondary_score"] >= min_score]

    # 按二次分降序
    traders.sort(key=lambda x: x["secondary_score"], reverse=True)

    return JSONResponse(
        content={
            "traders": traders,
            "count": len(traders),
            "tag_query": tag,
            "niche_only": bool(niche_only),
            "warning": (
                (
                    "PolyHub的标签分类基于历史数据，部分交易者近期实际持仓可能包含其他类别。"
                    "建议点击钱包地址在Polymarket验证后再跟投。"
                )
                if not niche_only
                else None
            ),
        }
    )


@router.get("/api/polyhub/trader")
async def polyhub_trader_detail(
    user_id: str = Query(..., description="Trader 钱包地址（0x...）"),
) -> JSONResponse:
    """代理 PolyHub /api/v1/traders/by-address 获取单个 trader 详情"""
    if not user_id.startswith("0x"):
        raise HTTPException(status_code=400, detail="user_id 必须以 0x 开头")
    url = f"{_polyhub_base()}/api/v1/traders/by-address"
    try:
        resp = requests.get(
            url, params={"user_id": user_id}, timeout=_polyhub_timeout()
        )
        resp.raise_for_status()
        raw = resp.json()
        # 标准化字段名（camelCase → snake_case）
        trader = _normalize_trader(raw) if isinstance(raw, dict) else raw
        trader["secondary_score"] = _compute_secondary_score(trader)
        return JSONResponse(content=trader)
    except requests.RequestException as exc:
        logger.warning("PolyHub trader detail fetch failed: %s", exc)
        raise HTTPException(
            status_code=502, detail=f"PolyHub API error: {exc}"
        ) from exc


# ── Copy Tasks CRUD ───────────────────────────────────────────────────


class CopyTaskCreate(BaseModel):
    wallet_address: str
    categories: list[str] = []
    base_usdc: float = 10.0
    niche_only: bool = True
    score_threshold: float = 75.0
    tag_label: str = ""


class CopyTaskPatch(BaseModel):
    status: str | None = None  # running | paused | stopped
    base_usdc: float | None = None
    categories: list[str] | None = None
    niche_only: bool | None = None
    score_threshold: float | None = None


@router.get("/api/copy-tasks")
async def list_copy_tasks() -> JSONResponse:
    """列出全部跟投任务"""
    try:

        def _db():
            with db_connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM copy_tasks ORDER BY created_at DESC"
                ).fetchall()
            return [dict(r) for r in rows]

        tasks = await asyncio.to_thread(_db)
        return JSONResponse(content={"tasks": tasks, "count": len(tasks)})
    except Exception as exc:
        logger.exception("list_copy_tasks error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/api/copy-tasks")
async def create_copy_task(payload: CopyTaskCreate, request: Request) -> JSONResponse:
    """创建一个跟投任务（若该钱包已有运行中任务则返回 409）

    T901: 跟投任务是全局共享对象且会驱动真实跟投资金，
    因此仅 admin / super_admin 可创建。
    """
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    wallet = payload.wallet_address.strip().lower()
    if not wallet.startswith("0x"):
        raise HTTPException(status_code=400, detail="wallet_address 必须以 0x 开头")

    now_iso = datetime.now(timezone.utc).isoformat()
    categories_json = json.dumps(payload.categories)

    try:

        def _db():
            with db_connect() as conn:
                existing = conn.execute(
                    "SELECT id, status FROM copy_tasks WHERE wallet_address=? AND status IN ('running', 'paused')",
                    (wallet,),
                ).fetchone()
                if existing:
                    status_label = (
                        "运行中" if existing["status"] == "running" else "已暂停"
                    )
                    raise HTTPException(
                        status_code=409,
                        detail=f"钱包 {wallet} 已有{status_label}跟投任务（id={existing['id']}），请先停止该任务",
                    )

                cur = conn.execute(
                    """INSERT INTO copy_tasks
                   (wallet_address, categories, base_usdc, niche_only, score_threshold,
                    status, created_at, last_active_at, tag_label)
                   VALUES (?,?,?,?,?,  'running',?,?,?) RETURNING id""",
                    (
                        wallet,
                        categories_json,
                        payload.base_usdc,
                        1 if payload.niche_only else 0,
                        payload.score_threshold,
                        now_iso,
                        now_iso,
                        payload.tag_label,
                    ),
                )
                row = cur.fetchone()
                return row["id"] if row else None

        task_id = await asyncio.to_thread(_db)
        logger.info("Created copy task id=%s for wallet=%s", task_id, wallet)
        return JSONResponse(content={"ok": True, "task_id": task_id}, status_code=201)
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("create_copy_task error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.patch("/api/copy-tasks/{task_id}")
async def patch_copy_task(
    task_id: int, payload: CopyTaskPatch, request: Request
) -> JSONResponse:
    """更新跟投任务（状态/配置）。T901: 仅 admin / super_admin。"""
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    valid_statuses = {"running", "paused", "stopped"}
    if payload.status and payload.status not in valid_statuses:
        raise HTTPException(
            status_code=400, detail=f"status 必须是 {valid_statuses} 之一"
        )

    updates: list[str] = []
    params: list[Any] = []

    if payload.status is not None:
        updates.append("status=?")
        params.append(payload.status)
    if payload.base_usdc is not None:
        updates.append("base_usdc=?")
        params.append(payload.base_usdc)
    if payload.categories is not None:
        updates.append("categories=?")
        params.append(json.dumps(payload.categories))
    if payload.niche_only is not None:
        updates.append("niche_only=?")
        params.append(1 if payload.niche_only else 0)
    if payload.score_threshold is not None:
        updates.append("score_threshold=?")
        params.append(payload.score_threshold)

    if not updates:
        raise HTTPException(status_code=400, detail="未提供任何更新字段")

    params.append(task_id)
    try:

        def _db():
            with db_connect() as conn:
                conn.execute(
                    f"UPDATE copy_tasks SET {', '.join(updates)} WHERE id=?", params
                )

        await asyncio.to_thread(_db)
        # 构建人类可读的变更日志（避免显示SQL模板 'status=?' 误导用户）
        readable = {u.split("=")[0]: p for u, p in zip(updates, params[:-1])}
        logger.info("Patched copy task id=%s: %s", task_id, readable)
        return JSONResponse(content={"ok": True})
    except Exception as exc:
        logger.exception("patch_copy_task error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.delete("/api/copy-tasks/{task_id}")
async def delete_copy_task(task_id: int, request: Request) -> JSONResponse:
    """删除跟投任务。T901: 仅 admin / super_admin。"""
    identity = get_request_identity(request)
    if not identity or not identity.get("is_admin"):
        return JSONResponse({"error": "admin required"}, status_code=403)

    try:

        def _db():
            with db_connect() as conn:
                cur = conn.execute("DELETE FROM copy_tasks WHERE id=?", (task_id,))
                return cur.rowcount

        rowcount = await asyncio.to_thread(_db)
        if rowcount == 0:
            raise HTTPException(status_code=404, detail=f"任务 id={task_id} 不存在")
        logger.info("Deleted copy task id=%s", task_id)
        return JSONResponse(content={"ok": True})
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("delete_copy_task error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
