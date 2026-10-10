"""T873 — OddsPapi 元数据 / 期货 / 事件富化 公共 API 路由。

端点（全部 GET，60s+ HTTP 缓存）：
- /api/oddsapi/meta/{type}     公共参考数据（bookmakers/sports/markets/...）
- /api/oddsapi/meta            汇总缓存清单
- /api/oddsapi/futures         期货赛事清单
- /api/oddsapi/futures/odds    单个期货赔率
- /api/oddsapi/event/{sport}/{fixture_id}
                               读取本地 event_store（含 odds/clv/history/settlement/media）
- /api/oddsapi/refresh-meta    管理员强制刷新单 type 缓存

所有写入端点（refresh-meta）需 admin；其余 GET 进入 web_dashboard 公共白名单。
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, HTMLResponse

from scripts.dashboard_routes._shared import get_request_identity, logger

router = APIRouter()

_VALID_META_TYPES: tuple[str, ...] = (
    "sports",
    "bookmakers",
    "markets",
    "currencies",
    "venues",
)
_VALID_PARAM_META_TYPES: tuple[str, ...] = (
    "tournaments",
    "seasons",
    "participants",
)


def _is_admin(request: Request) -> bool:
    ident = get_request_identity(request) or {}
    return bool(ident.get("authenticated") and ident.get("is_admin"))


def _meta_fetch_dispatch(meta_type: str, sport_id: int, tournament_id: int):
    """T873 — 把 meta_type 映射到 odds_fetcher 的具体 fetcher。"""
    from src.utils import odds_fetcher as of

    if meta_type == "sports":
        return of.fetch_sports_metadata
    if meta_type == "bookmakers":
        return of.fetch_bookmakers
    if meta_type == "markets":
        return lambda: of.fetch_markets(sport_id=sport_id)
    if meta_type == "currencies":
        return of.fetch_currencies
    if meta_type == "venues":
        return lambda: of.fetch_venues(sport_id=sport_id)
    if meta_type == "tournaments":
        return lambda: of.fetch_tournaments(sport_id=sport_id)
    if meta_type == "seasons":
        return lambda: of.fetch_seasons(tournament_id=tournament_id)
    if meta_type == "participants":
        return lambda: of.fetch_participants(tournament_id=tournament_id)
    return None


def _cache_name(meta_type: str, sport_id: int, tournament_id: int) -> str:
    if meta_type in ("tournaments",):
        return f"tournaments_{sport_id}"
    if meta_type in ("seasons",):
        return f"seasons_{tournament_id}"
    if meta_type in ("participants",):
        return f"participants_{tournament_id}"
    if meta_type in ("markets", "venues") and sport_id > 0:
        return f"{meta_type}_{sport_id}"
    return meta_type


@router.get("/api/oddsapi/meta")
def meta_index() -> JSONResponse:
    """汇总当前所有 meta 缓存条目。"""
    from src.utils import oddsapi_meta_store as ms

    return JSONResponse({"ok": True, **ms.stats()})


@router.get("/api/oddsapi/meta/{meta_type}")
def meta_get(
    meta_type: str,
    request: Request,
    sport_id: int = 0,
    tournament_id: int = 0,
    force: int = 0,
) -> JSONResponse:
    """读取（或惰性拉取）某种 meta 数据。

    - sport_id/tournament_id 仅在对应 type 下有意义
    - force=1 时仅 admin 可强制刷新
    """
    if meta_type not in (*_VALID_META_TYPES, *_VALID_PARAM_META_TYPES):
        return JSONResponse(
            {"ok": False, "error": "invalid_meta_type", "type": meta_type},
            status_code=400,
        )
    if meta_type in _VALID_PARAM_META_TYPES:
        if meta_type == "tournaments" and sport_id <= 0:
            return JSONResponse(
                {"ok": False, "error": "sport_id_required"}, status_code=400
            )
        if meta_type in ("seasons", "participants") and tournament_id <= 0:
            return JSONResponse(
                {"ok": False, "error": "tournament_id_required"}, status_code=400
            )

    from src.utils import oddsapi_meta_store as ms

    cache_name = _cache_name(meta_type, sport_id, tournament_id)
    fetch_fn = _meta_fetch_dispatch(meta_type, sport_id, tournament_id)
    if fetch_fn is None:
        return JSONResponse(
            {"ok": False, "error": "fetch_unavailable"}, status_code=500
        )
    do_force = bool(force) and _is_admin(request)
    data = ms.get_or_fetch(cache_name, fetch_fn, force=do_force)
    return JSONResponse(
        {
            "ok": True,
            "type": meta_type,
            "cache": cache_name,
            "count": (len(data) if isinstance(data, (list, dict)) else 0),
            "data": data or [],
            "ts": int(time.time()),
        }
    )


@router.post("/api/oddsapi/refresh-meta")
def meta_refresh(
    meta_type: str,
    request: Request,
    sport_id: int = 0,
    tournament_id: int = 0,
) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return meta_get(
        meta_type, request, sport_id=sport_id, tournament_id=tournament_id, force=1
    )


@router.get("/api/oddsapi/futures")
def futures_list(sport_id: int = 0) -> JSONResponse:
    """T873 — 期货赛事清单（缓存 6h）。"""
    from src.utils import odds_fetcher as of

    data = of.fetch_futures(sport_id=sport_id)
    return JSONResponse(
        {
            "ok": True,
            "sport_id": sport_id,
            "count": len(data),
            "data": data,
            "ts": int(time.time()),
        }
    )


@router.get("/api/oddsapi/futures/odds")
def futures_odds(future_id: str, bookmakers: str = "") -> JSONResponse:
    """T873 — 单个期货赔率（缓存 6h）。"""
    if not future_id:
        return JSONResponse(
            {"ok": False, "error": "future_id_required"}, status_code=400
        )
    from src.utils import odds_fetcher as of

    data = of.fetch_futures_odds(future_id, bookmakers=bookmakers)
    return JSONResponse(
        {
            "ok": True,
            "future_id": future_id,
            "data": data or {},
            "ts": int(time.time()),
        }
    )


@router.get("/api/oddsapi/event/{sport_key}/{fixture_id}")
def event_detail(sport_key: str, fixture_id: str) -> JSONResponse:
    """T873 — 读取 event_store 中单事件的全部缓存数据（odds/clv/history/...）。"""
    from src.utils import oddsapi_event_store as es

    ev = es.get_event(sport_key, fixture_id)
    if not ev:
        return JSONResponse(
            {"ok": False, "error": "not_found"}, status_code=404
        )
    return JSONResponse({"ok": True, "event": ev})


@router.post("/api/oddsapi/event/{sport_key}/{fixture_id}/enrich")
def event_enrich(
    sport_key: str,
    fixture_id: str,
    request: Request,
    fields: str = "clv,history,media,settlement",
) -> JSONResponse:
    """T873 — admin 触发：对单 fixture 拉取 CLV / historical / media / settlement
    并 attach 到 event_store。fields 用逗号分隔。
    """
    if not _is_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    from src.utils import odds_fetcher as of
    from src.utils import oddsapi_event_store as es

    wanted = {s.strip().lower() for s in (fields or "").split(",") if s.strip()}
    out: dict[str, Any] = {"sport_key": sport_key, "fixture_id": fixture_id, "applied": {}}

    if "clv" in wanted:
        clv = of.fetch_fixture_clv(fixture_id)
        if clv is not None:
            es.attach_clv(sport_key, fixture_id, clv)
            out["applied"]["clv"] = True
        else:
            out["applied"]["clv"] = False

    if "history" in wanted:
        snap = of.fetch_fixture_historical(fixture_id)
        if snap is not None:
            es.append_history(sport_key, fixture_id, snap)
            out["applied"]["history"] = True
        else:
            out["applied"]["history"] = False

    if "media" in wanted:
        media = of.fetch_fixture_media(fixture_id)
        if media is not None:
            es.attach_media(sport_key, fixture_id, media)
            out["applied"]["media"] = True
        else:
            out["applied"]["media"] = False

    if "settlement" in wanted:
        st = of.fetch_fixture_settlement(fixture_id)
        if st is not None:
            es.attach_settlement(sport_key, fixture_id, st)
            out["applied"]["settlement"] = True
        else:
            out["applied"]["settlement"] = False

    return JSONResponse({"ok": True, **out})


@router.post("/api/oddsapi/settlement/backfill")
def settlement_backfill(request: Request, sport_key: str = "", limit: int = 50) -> JSONResponse:
    """T873 — admin 触发：扫描 archive 中无 settlement 的事件，调 /fixtures/settlement
    回填真实赢家。受 limit 上限保护避免一次性爆配额。
    """
    if not _is_admin(request):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    from src.utils import odds_fetcher as of
    from src.utils import oddsapi_event_store as es

    items = es.list_archive(sport_key=(sport_key or None) or None)
    pending = [it for it in items if not it.get("has_settlement")]
    pending = pending[: max(1, min(int(limit or 50), 200))]

    n_ok = 0
    n_fail = 0
    for it in pending:
        sk = it.get("sport_key") or ""
        fid = it.get("fixture_id") or ""
        if not sk or not fid:
            continue
        try:
            st = of.fetch_fixture_settlement(str(fid))
        except Exception as exc:
            logger.warning(f"[oddsapi backfill] fetch failed {sk}/{fid}: {exc}")
            n_fail += 1
            continue
        if st is None:
            n_fail += 1
            continue
        es.attach_settlement(sk, str(fid), st)
        n_ok += 1
    return JSONResponse(
        {
            "ok": True,
            "scanned": len(pending),
            "attached": n_ok,
            "failed": n_fail,
        }
    )


# ─── /futures SSR 页面 ───────────────────────────────────────────────
alias_router = APIRouter()


@alias_router.get("/futures")
@alias_router.get("/v2/futures")
def futures_page(request: Request) -> HTMLResponse:
    from fastapi.templating import Jinja2Templates
    import os as _os

    tpl_dir = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "frontend",
        "templates",
    )
    templates = Jinja2Templates(directory=tpl_dir)
    sport_id = 0
    try:
        sport_id = int(request.query_params.get("sport_id") or 0)
    except Exception:
        sport_id = 0
    initial: list = []
    try:
        from src.utils import odds_fetcher as of
        initial = of.fetch_futures(sport_id=sport_id) or []
    except Exception as exc:
        logger.warning(f"[futures] SSR initial fetch failed: {exc}")
    # 复用 frontend_routes 的共享模板上下文（i18n_dict / lang / nav_menu_items 等）。
    try:
        from scripts.dashboard_routes.frontend_routes import _tpl_ctx
        ctx = _tpl_ctx(request)
    except Exception as exc:
        logger.warning(f"[futures] _tpl_ctx unavailable: {exc}")
        ctx = {}
    ctx.update({
        "initial_items": initial,
        "initial_sport_id": sport_id,
    })
    return templates.TemplateResponse(request, "futures.html", ctx)


# ─── /oddsapi/fixture SSR 页面（T874: CLV + history 折线图） ────────
@alias_router.get("/oddsapi/fixture/{sport_key}/{fixture_id}")
def fixture_detail_page(sport_key: str, fixture_id: str, request: Request) -> HTMLResponse:
    """T874: 单 fixture 的 CLV + history 可视化。
    100% 读取本地 event_store JSON，零 OddsPapi 在线调用。
    """
    from fastapi.templating import Jinja2Templates
    import os as _os

    tpl_dir = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "frontend",
        "templates",
    )
    templates = Jinja2Templates(directory=tpl_dir)

    ev = None
    try:
        from src.utils import oddsapi_event_store as es
        ev = es.get_event(sport_key, fixture_id)
    except Exception as exc:
        logger.warning(f"[fixture] read failed sk={sport_key} fid={fixture_id}: {exc}")

    try:
        from scripts.dashboard_routes.frontend_routes import _tpl_ctx
        ctx = _tpl_ctx(request)
    except Exception:
        ctx = {}
    ctx.update({
        "sport_key": sport_key,
        "fixture_id": fixture_id,
        "event": ev or {},
        "has_event": bool(ev),
    })
    return templates.TemplateResponse(request, "fixture_detail.html", ctx)
