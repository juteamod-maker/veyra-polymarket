"""T872 — 企业级全站搜索（基于 PM gamma `/public-search`）。

旧实现：顶部搜索框走 `/api/pm/events?search=`，但 gamma 的 `events?search=` 并非
真正全文检索，会按 `volume24hr` 倒序返回与关键词无关的高热度事件 → 用户搜
"avispa" 实际拿到 Macron / Kraken 等无关结果，且 J-League / 小联赛 / 大量长尾
赛事根本不在 200 条候选池中，搜索全无意义。

新实现：直接代理 PM gamma `/public-search?q=...&events_status=active`，这是
PM 自己的全文搜索端点，命中率 100%。同时提供：
- `GET /api/pm/search?q=&page=&limit=` 列表 API（JSON）
- `GET /search?q=` 服务端渲染搜索结果页（独立 URL，可分享/收藏）
"""

from __future__ import annotations

import time
from threading import Lock
from typing import Any

import requests as _requests
from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from scripts.dashboard_routes._shared import logger

router = APIRouter()
alias_router = APIRouter()


_PM_PUBLIC_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
_PM_SEARCH_TIMEOUT = 5
# 服务端按 (q, page, limit, status) 缓存 60s — 多用户同 query 共用结果
_search_cache: dict[str, tuple[float, dict]] = {}
_search_cache_lock = Lock()
_SEARCH_CACHE_TTL = 60


def _trim_event(ev: dict) -> dict:
    """剔除 Web 不需要的大字段，瘦身 20× —— 详情仍可点 /event/<slug> 拉全。"""
    if not isinstance(ev, dict):
        return {}
    out = {
        "slug": ev.get("slug") or "",
        "title": ev.get("title") or "",
        "image": ev.get("image") or ev.get("icon") or "",
        "icon": ev.get("icon") or "",
        "endDate": ev.get("endDate") or ev.get("end_date") or "",
        "startDate": ev.get("startDate") or ev.get("start_date") or "",
        "volume24hr": ev.get("volume24hr") or 0,
        "volume": ev.get("volume") or 0,
        "liquidity": ev.get("liquidity") or 0,
        "openInterest": ev.get("openInterest") or 0,
        "active": bool(ev.get("active")),
        "closed": bool(ev.get("closed")),
        "tags": [
            (t.get("slug") or t.get("label") or "")
            for t in (ev.get("tags") or [])
            if isinstance(t, dict)
        ],
    }
    # markets 摘要：仅 question / yesPrice / outcomePrices —— 列表卡片够用
    mkts = []
    for m in (ev.get("markets") or [])[:6]:
        if not isinstance(m, dict):
            continue
        mkts.append(
            {
                "question": m.get("question") or "",
                "slug": m.get("slug") or "",
                "conditionId": m.get("conditionId") or "",
                "outcomePrices": m.get("outcomePrices") or "",
                "outcomes": m.get("outcomes") or "",
                "active": bool(m.get("active")),
                "closed": bool(m.get("closed")),
            }
        )
    out["markets"] = mkts
    out["market_count"] = len(ev.get("markets") or [])
    return out


def _do_public_search(
    q: str, page: int, limit: int, events_status: str
) -> dict:
    cache_key = f"{q}|{page}|{limit}|{events_status}".lower()
    now = time.time()
    with _search_cache_lock:
        cached = _search_cache.get(cache_key)
        if cached and now - cached[0] < _SEARCH_CACHE_TTL:
            return cached[1]
    try:
        r = _requests.get(
            _PM_PUBLIC_SEARCH_URL,
            params={
                "q": q,
                "limit_per_type": limit,
                "events_status": events_status,
                "page": page,
            },
            timeout=_PM_SEARCH_TIMEOUT,
        )
        if r.status_code != 200:
            payload = {
                "events": [],
                "pagination": {"hasMore": False, "totalResults": 0},
                "error": f"upstream_status_{r.status_code}",
            }
        else:
            data = r.json() or {}
            events = [_trim_event(e) for e in (data.get("events") or [])]
            payload = {
                "events": events,
                "pagination": data.get("pagination")
                or {"hasMore": False, "totalResults": len(events)},
                "tags": data.get("tags") or [],
                "profiles": data.get("profiles") or [],
            }
    except Exception as exc:
        logger.warning(f"[search] public-search failed q={q!r}: {exc}")
        payload = {
            "events": [],
            "pagination": {"hasMore": False, "totalResults": 0},
            "error": "exception",
        }
    with _search_cache_lock:
        _search_cache[cache_key] = (now, payload)
    return payload


@router.get("/api/pm/search")
async def pm_search(
    q: str = Query("", min_length=0, max_length=120),
    page: int = Query(1, ge=1, le=50),
    limit: int = Query(20, ge=1, le=50),
    status: str = Query("active", pattern="^(active|resolved|all)$"),
) -> JSONResponse:
    """T872 — 全站搜索 API（PM gamma /public-search 代理 + 60s 缓存）。

    Query params:
    - q: 搜索关键词（≥1 字符；空字符串返回空集，不触发上游）
    - page: 1-based 页号（PM gamma `page=` 语义）
    - limit: 每页条数（≤50；上游 `limit_per_type`）
    - status: active / resolved / all（映射到 events_status）
    """
    qn = (q or "").strip()
    if not qn:
        return JSONResponse(
            {
                "q": "",
                "events": [],
                "pagination": {"hasMore": False, "totalResults": 0},
            }
        )
    payload = _do_public_search(qn, page=page, limit=limit, events_status=status)
    payload["q"] = qn
    payload["page"] = page
    payload["limit"] = limit
    payload["status"] = status
    return JSONResponse(payload)


def _search_page_response(request: Request) -> HTMLResponse:
    """T872 — 服务端渲染搜索结果页 /search?q=<query>。"""
    # Lazy import to avoid circular deps with frontend_routes which already
    # owns the templates env + ctx helper.
    from scripts.dashboard_routes.frontend_routes import (
        templates as _tpls,
        _tpl_ctx as _ctx,
        _apply_runtime_cookie as _apply_cookie,
    )

    ctx = _ctx(request)
    qp = request.query_params
    q = (qp.get("q") or "").strip()[:120]
    try:
        page = max(1, min(50, int(qp.get("page") or 1)))
    except Exception:
        page = 1
    try:
        limit = max(1, min(50, int(qp.get("limit") or 20)))
    except Exception:
        limit = 20
    status = (qp.get("status") or "active").strip().lower()
    if status not in {"active", "resolved", "all"}:
        status = "active"

    # SSR 首屏：仅在有 query 时拉；否则空状态页
    ssr_payload: dict[str, Any] = {
        "events": [],
        "pagination": {"hasMore": False, "totalResults": 0},
    }
    if q:
        ssr_payload = _do_public_search(q, page=page, limit=limit, events_status=status)
    ctx["search_q"] = q
    ctx["search_page"] = page
    ctx["search_limit"] = limit
    ctx["search_status"] = status
    ctx["search_ssr_payload"] = ssr_payload
    resp = _tpls.TemplateResponse(request, "search.html", ctx)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    _apply_cookie(request, resp)
    return resp


@router.get("/v2/search", response_class=HTMLResponse)
async def search_page_v2(request: Request):
    return _search_page_response(request)


@alias_router.get("/search", response_class=HTMLResponse)
async def search_page_alias(request: Request):
    return _search_page_response(request)
