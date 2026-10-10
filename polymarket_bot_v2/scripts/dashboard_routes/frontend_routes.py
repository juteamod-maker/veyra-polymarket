"""
frontend_routes.py — V2 前端路由（TailAdmin 暗色主题）
挂载在 /v2 前缀下，使用 Jinja2 模板渲染。
"""

import hashlib
import json
import time
import logging
import os
import asyncio
import re
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from scripts.dashboard_routes._shared import (
    db_connect,
    escape_script_json,
    get_request_identity,
    verify_user_session,
)

logger = logging.getLogger("web_dashboard")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
TEMPLATES_DIR = FRONTEND_DIR / "templates"
LEGACY_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
# T901: 模板中嵌入 <script> 的 JSON 统一走 escape_script_json，防止 </script> 逃逸 XSS。
templates.env.filters["escape_script_json"] = escape_script_json

router = APIRouter(prefix="/v2", tags=["frontend-v2"])

alias_router = APIRouter(tags=["frontend-v2-alias"])

_DASHBOARD_KEY = (
    ""  # T474: dashboard_key removed; admin gated solely via user_session role.
)

_I18N_DICT_CACHE: dict[str, tuple[float, dict]] = {}


def _resolve_seaz_entry_assets() -> tuple[str, str]:
    assets_dir = FRONTEND_DIR / "static" / "seaz" / "assets"
    if not assets_dir.exists():
        return "", ""

    def _pick_latest(patterns: list[str]) -> str:
        candidates: list[Path] = []
        for pattern in patterns:
            candidates.extend(assets_dir.glob(pattern))
        if not candidates:
            return ""
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        version = int(latest.stat().st_mtime)
        return f"/v2/static/seaz/assets/{latest.name}?v={version}"

    entry_js = _pick_latest(["index-*.js", "index.js"])
    entry_css = _pick_latest(["index-*.css", "index.css"])
    return entry_js, entry_css


def _load_i18n_dict(lang: str) -> dict:
    path = FRONTEND_DIR / "static" / "i18n" / f"{lang}.json"
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
        cached = _I18N_DICT_CACHE.get(lang)
        if cached and cached[0] == mtime:
            return cached[1]
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        _I18N_DICT_CACHE[lang] = (mtime, data)
        return data
    except Exception:
        return {}


def _asset_version() -> str:
    """Return short hash based on key frontend assets mtimes for cache-busting."""
    # T853 性能专项：30s TTL 进程内缓存，命中时直接返回，跳过 30+ stat() + MD5。
    global _ASSET_VER_CACHE
    _now = time.time()
    if _ASSET_VER_CACHE is not None and _now - _ASSET_VER_CACHE[0] < 30.0:
        return _ASSET_VER_CACHE[1]
    targets = [
        FRONTEND_DIR / "templates" / "base.html",
        FRONTEND_DIR / "templates" / "trades.html",
        FRONTEND_DIR / "static" / "js" / "app.js",
        FRONTEND_DIR / "static" / "js" / "realtime-price.js",
        FRONTEND_DIR / "static" / "js" / "pm_guard.js",
        FRONTEND_DIR / "static" / "js" / "vp_ui.js",
        FRONTEND_DIR / "static" / "js" / "i18n.js",
        FRONTEND_DIR / "static" / "js" / "runtime_loader.js",
        FRONTEND_DIR / "static" / "js" / "runtime" / "htmx_bootstrap.js",
        FRONTEND_DIR / "static" / "js" / "runtime" / "htmx_pages" / "go.js",
        FRONTEND_DIR / "static" / "js" / "runtime" / "htmx_pages" / "deposit.js",
        FRONTEND_DIR / "static" / "js" / "runtime" / "htmx_pages" / "refer.js",
        FRONTEND_DIR / "static" / "js" / "runtime" / "htmx_pages" / "withdraw.js",
        # T524 #524/#525/#526 — these per-page modules previously lacked
        # cache-busting because they weren't part of the hash basis. As a
        # result hot-fixes shipped to event_detail.js / discover.js / pm_guard
        # rendered nothing for already-cached browsers. Include them so any
        # change forces clients onto the freshly deployed bundle.
        FRONTEND_DIR / "static" / "js" / "runtime" / "htmx_pages" / "event_detail.js",
        FRONTEND_DIR / "static" / "js" / "runtime" / "htmx_pages" / "discover.js",
        FRONTEND_DIR / "static" / "js" / "runtime" / "htmx_pages" / "valuelab.js",
        FRONTEND_DIR
        / "static"
        / "js"
        / "runtime"
        / "htmx_pages"
        / "valuelab_detail.js",
        # T610B — leaderboard 页面 vanilla JS + 模板，未列入会导致 hot-fix 被 CF/浏览器旧缓存截留
        FRONTEND_DIR / "static" / "js" / "runtime" / "pages" / "leaderboard.js",
        FRONTEND_DIR / "templates" / "leaderboard.html",
        FRONTEND_DIR / "templates" / "valuelab.html",
        FRONTEND_DIR / "templates" / "valuelab_detail.html",
        FRONTEND_DIR / "static" / "js" / "vendor" / "marked.min.js",
        FRONTEND_DIR / "static" / "js" / "vendor" / "purify.min.js",
        FRONTEND_DIR / "static" / "js" / "vendor" / "htmx.min.js",
        FRONTEND_DIR / "static" / "i18n" / "zh-CN.json",
        FRONTEND_DIR / "static" / "i18n" / "en.json",
        FRONTEND_DIR / "static" / "css" / "vp_ui.css",
        # T838/T840 — coin drop assets must invalidate cache when patched
        FRONTEND_DIR / "static" / "js" / "coin_drop.js",
        FRONTEND_DIR / "static" / "css" / "coin_drop.css",
        # T906 — Privy signing runtime is hot-patched (preload warmup, signer
        # mismatch fallback). Without inclusion the asset_version hash does
        # not change and old browsers/CF edge keep serving the previous JS,
        # leaving the 10-25s wallet popup delay in place.
        FRONTEND_DIR / "static" / "js" / "runtime" / "privy_signing_runtime.js",
        # T907 — Polymarket trading session orchestrator (browser-side, official
        # @polymarket/builder-relayer-client + @polymarket/clob-client-v2 SDKs).
        FRONTEND_DIR / "static" / "js" / "runtime" / "polymarket_session.js",
        # T907 — Bundled @polymarket/* TS SDKs (esbuild output). Cache-bust
        # must rotate when we rebuild the bundle.
        FRONTEND_DIR / "static" / "js" / "runtime" / "polymarket_sdk_bundle.js",
        # T839+/T844/T845 — seaz preview bundle is hand-patched in place;
        # include it so cache-bust hash changes when we re-patch the bundle.
        FRONTEND_DIR / "static" / "seaz" / "assets" / "index-dYjrTTEm.js",
        FRONTEND_DIR / "templates" / "weather_event_map.html",
        FRONTEND_DIR / "templates" / "withdraw.html",
        # T855/T856 — unified landing page assets (helmetz-cloned, viewport card)
        FRONTEND_DIR / "templates" / "landing.html",
        FRONTEND_DIR / "static" / "css" / "landing.css",
        FRONTEND_DIR / "static" / "js" / "landing" / "landing.js",
        LEGACY_STATIC_DIR / "js" / "bets.js",
        LEGACY_STATIC_DIR / "css" / "betslist.css",
    ]
    try:
        mtimes = [str(int(p.stat().st_mtime)) for p in targets if p.exists()]
        if not mtimes:
            result = "0"
        else:
            result = hashlib.md5("|".join(mtimes).encode()).hexdigest()[:10]
    except Exception:
        result = "0"
    _ASSET_VER_CACHE = (time.time(), result)
    return result


def _is_admin(request: Request) -> bool:
    """T474: admin = user_session cookie role in {admin, super_admin}."""
    user_session_token = request.cookies.get("user_session", "")
    user_info = verify_user_session(user_session_token) if user_session_token else None
    user_role = str((user_info or {}).get("role") or "").strip().lower()
    return user_role in {"admin", "super_admin"}


def _detect_lang(request: Request) -> str:
    """T346 + T490: 服务端首屏语言检测。
    优先级: cookie veyra_lang > Accept-Language（解析 q 权重，匹配支持的语种）> en
    支持的语种: zh-CN / en
    默认改为 en，让全球用户首屏即英文，本地中文用户由浏览器 Accept-Language 自动命中 zh。
    """
    raw = (request.cookies.get("veyra_lang") or "").strip()
    if raw in ("en", "zh-CN"):
        return raw
    accept = (request.headers.get("accept-language") or "").strip()
    if accept:
        # 解析 "zh-CN,zh;q=0.9,en;q=0.8" 形式，挑选最高权重的支持语
        best = None
        best_q = -1.0
        for part in accept.split(","):
            seg = part.strip()
            if not seg:
                continue
            tag, _, q_part = seg.partition(";")
            tag = tag.strip().lower()
            try:
                q = (
                    float(q_part.split("=", 1)[1])
                    if q_part.lower().startswith("q=")
                    else 1.0
                )
            except Exception:
                q = 1.0
            mapped = None
            if tag.startswith("zh"):
                mapped = "zh-CN"
            elif tag.startswith("en"):
                mapped = "en"
            if mapped and q > best_q:
                best = mapped
                best_q = q
        if best:
            return best
    return "en"


def _resolve_frontend_runtime(request: Request) -> str:
    """V2 frontend runtime is HTMX-only."""
    return "htmx"


# T611 — SSR 侧栏菜单进程内 TTL 缓存
# 每次 SSR 都同步查 PostgreSQL 会阻塞 async event loop，对 /v2 任何页面都
# 是隐藏放大器：admin 打开 DBA tab 时，并发的导航/marquee 请求恰好命中
# 同一 worker 就会拖慢整个面板。菜单写操作命中 /api/menu/admin/* 时主动
# invalidate 即可保证 UI 实时性。
_NAV_MENU_CACHE: dict[bool, tuple[float, list[dict]]] = {}
# T853 性能专项：asset_version 进程内缓存（30s TTL）
_ASSET_VER_CACHE: tuple[float, str] | None = None
# T853 性能专项：onboarding DB check 每钱包 60s TTL 缓存
_ONBOARDING_CACHE: dict[str, tuple[float, bool]] = {}
_ONBOARDING_TTL_SEC = 60.0
# T603C — admin 反馈"菜单管理无缓存、必须实时"，原 TTL=0；
# T853 性能专项：写操作已主动调用 invalidate_nav_menu_cache() 保证实时性，
# 正常 SSR 提升至 30s TTL，消除每次 SSR 都同步查 SQLite 的开销。
_NAV_MENU_TTL_SEC = 30.0

_RAW_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def invalidate_nav_menu_cache() -> None:
    """供 menu_routes 的写操作调用，立即失效 SSR 缓存。"""
    _NAV_MENU_CACHE.clear()


def _load_nav_menu_items(request: Request) -> list[dict]:
    """T603B — SSR 注入侧栏菜单，移除 base.html 中的硬编码 <a> 列表。

    后台 admin 在「应用菜单管理」里改动后，下次任意页面 SSR 直接看到新菜单；
    JS 端 hydrateMenu 仍负责后续 i18n 切换 / 实时广播刷新。

    过滤规则与 /api/menu/list 完全一致：visible=TRUE，role_required=admin
    仅 admin 可见；其它（含 NULL/'user'/'guest'）所有人可见。

    T611 — 加 30s 进程内 TTL，写操作显式 invalidate。
    """
    try:
        ident = get_request_identity(request)
    except Exception:
        ident = None
    is_admin = bool((ident or {}).get("is_admin"))
    now = time.time()
    cached = _NAV_MENU_CACHE.get(is_admin)
    if cached and (now - cached[0]) < _NAV_MENU_TTL_SEC:
        return cached[1]
    try:
        with db_connect() as conn:
            cursor = conn.execute(
                "SELECT id,label,path,icon,sort_order,visible,role_required,section_label,"
                "i18n_key,label_zh,label_en "
                "FROM app_menus WHERE visible = TRUE ORDER BY sort_order ASC, id ASC"
            )
            rows = cursor.fetchall()
    except Exception as e:
        logger.warning(f"[ssr-nav-menu] {e}")
        return []
    items: list[dict] = []
    for r in rows:
        # 与 menu_routes._row_to_menu 同形（这里独立实现避免循环 import）
        if isinstance(r, dict):
            row = r
        else:
            row = {
                "id": r[0],
                "label": r[1],
                "path": r[2],
                "icon": r[3] or "circle",
                "sort_order": int(r[4] or 0),
                "visible": bool(r[5]),
                "role_required": r[6] or "user",
                "section_label": r[7],
                "i18n_key": (r[8] if len(r) > 8 else None) or "",
                "label_zh": (r[9] if len(r) > 9 else None) or "",
                "label_en": (r[10] if len(r) > 10 else None) or "",
            }
        req_role = (row.get("role_required") or "user").lower()
        if req_role == "admin" and not is_admin:
            continue
        items.append(
            {
                "id": row.get("id"),
                "label": row.get("label") or "",
                "path": row.get("path") or "/",
                "icon": row.get("icon") or "circle",
                "sort_order": int(row.get("sort_order") or 0),
                "section_label": row.get("section_label") or "",
                "i18n_key": (row.get("i18n_key") or "").strip(),
                "label_zh": (row.get("label_zh") or "").strip(),
                "label_en": (row.get("label_en") or "").strip(),
            }
        )
    _NAV_MENU_CACHE[is_admin] = (now, items)
    return items


def _compute_onboarding_need_from_db(request: Request) -> bool:
    """Fallback for first-page render when session flag was not persisted yet.

    Primary source remains request.session['needs_onboarding'] (set at auth hook).
    This fallback only runs when the session key is missing.
    """
    try:
        ident = get_request_identity(request)
    except Exception:
        ident = None
    if not ident:
        return False

    role = str((ident or {}).get("role") or "").strip().lower()
    wallet = str((ident or {}).get("wallet_address") or "").strip().lower()
    if role != "manual_user" or not wallet or wallet == "__admin__":
        return False

    # T853 性能专项：每钱包 60s TTL 缓存，避免每次 SSR 同步查 SQLite。
    # onboarding 完成后 session["needs_onboarding"]=False 会将该分支短路，
    # 因此 60s 过期只影响首次登陆的极短窗口，不会造成 onboarding 入口残留。
    _now = time.time()
    _cached = _ONBOARDING_CACHE.get(wallet)
    if _cached is not None and _now - _cached[0] < _ONBOARDING_TTL_SEC:
        return _cached[1]

    try:
        with db_connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(display_name, '') AS display_name FROM users WHERE wallet_address=?",
                (wallet,),
            ).fetchone()
        display_name = str((row["display_name"] if row else "") or "").strip()
    except Exception as e:
        logger.warning("[onboarding-fallback] db read failed: %s", e)
        return False

    if not display_name:
        result = True
    elif display_name.lower() == wallet:
        result = True
    else:
        result = bool(_RAW_WALLET_RE.fullmatch(display_name))
    _ONBOARDING_CACHE[wallet] = (_now, result)
    return result


def _tpl_ctx(request: Request) -> dict:
    """Shared template context for all V2 pages."""
    lang = _detect_lang(request)
    i18n_dict = _load_i18n_dict(lang)
    i18n_en = _load_i18n_dict("en") if lang != "en" else i18n_dict
    frontend_runtime = _resolve_frontend_runtime(request)
    needs_onboarding = None
    try:
        needs_onboarding = request.session.get("needs_onboarding")
    except Exception:
        needs_onboarding = None

    db_needs_onboarding = None
    if needs_onboarding is None:
        needs_onboarding = _compute_onboarding_need_from_db(request)
        try:
            request.session["needs_onboarding"] = bool(needs_onboarding)
        except Exception:
            pass
    else:
        # Session can be stale (e.g. prior false persisted before display_name was reset).
        # Reconcile with DB truth so onboarding host can still be rendered on first page load.
        if not bool(needs_onboarding):
            db_needs_onboarding = _compute_onboarding_need_from_db(request)
            if db_needs_onboarding:
                needs_onboarding = True
                try:
                    request.session["needs_onboarding"] = True
                except Exception:
                    pass

    needs_onboarding = bool(needs_onboarding)

    return {
        "is_admin": _is_admin(request),
        "asset_version": _asset_version(),
        "frontend_runtime": frontend_runtime,
        "lang": lang,
        "i18n_dict": i18n_dict,
        "i18n_dict_json": json.dumps(i18n_dict, ensure_ascii=False),
        "i18n_en_json": json.dumps(i18n_en, ensure_ascii=False),
        # T907 hot-fix — base.html 中曾用 `{{...}}{% if %}, "en": {{...}}{% endif %}`
        # 拼接 preload 对象。HTML formatter 反复把相邻 `{{` 改写成 `{ {`，
        # 导致线上出现 SyntaxError。改为 Python 侧组装单一 JSON 对象，
        # 模板里只剩一个不可被破坏的 `{{ i18n_preload_json | safe }}`。
        "i18n_preload_json": json.dumps(
            ({lang: i18n_dict} if lang == "en" else {lang: i18n_dict, "en": i18n_en}),
            ensure_ascii=False,
        ),
        # T856追加 A — landing 页性能专项：landing.html 只用 `landing.*` 前缀
        # 的少量键值（约 60 项 / 6KB），却被注入了 1300 项全量字典两份（~140KB）。
        # 这里额外暴露已切片的精简 JSON 给 landing.html 的内联 preload 使用，
        # base.html 等其它模板继续用全量字段以避免回归。
        "landing_i18n_json": json.dumps(
            {k: v for k, v in i18n_dict.items() if k.startswith("landing.")},
            ensure_ascii=False,
        ),
        "landing_i18n_en_json": json.dumps(
            {k: v for k, v in i18n_en.items() if k.startswith("landing.")},
            ensure_ascii=False,
        ),
        # T907 hot-fix — same root cause as i18n_preload_json. Pre-assemble
        # so landing.html only embeds a single Jinja substitution.
        "landing_i18n_preload_json": (
            lambda _zh, _en: json.dumps(
                ({lang: _zh} if lang == "en" else {lang: _zh, "en": _en}),
                ensure_ascii=False,
            )
        )(
            {k: v for k, v in i18n_dict.items() if k.startswith("landing.")},
            {k: v for k, v in i18n_en.items() if k.startswith("landing.")},
        ),
        # T603B — SSR 侧栏菜单，base.html 不再硬编码菜单项。
        "nav_menu_items": _load_nav_menu_items(request),
        "needs_onboarding": needs_onboarding,
    }


def _apply_runtime_cookie(request: Request, resp: Response) -> None:
    """No-op: runtime overrides are intentionally disabled in V2."""
    return None


def _tpl_response(request: Request, template_name: str) -> HTMLResponse:
    """Build V2 template response with no-store headers to avoid stale HTML shell."""
    resp = templates.TemplateResponse(request, template_name, _tpl_ctx(request))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    _apply_runtime_cookie(request, resp)
    return resp


def _help_tpl_response(request: Request, slug: str) -> HTMLResponse:
    ctx = _tpl_ctx(request)
    ctx["help_slug"] = slug
    # T875-followup: Privacy/Terms/Help pages must be readable by users who
    # have not finished onboarding (otherwise the onboarding modal blocks the
    # very content they need to read in order to accept the agreement).
    ctx["needs_onboarding"] = False
    resp = templates.TemplateResponse(request, "go.html", ctx)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    _apply_runtime_cookie(request, resp)
    return resp


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """V2 仪表盘主页。"""
    return _tpl_response(request, "dashboard.html")


@router.get("/user", response_class=HTMLResponse)
@router.head("/user")
async def user_page(request: Request):
    """V2 用户资料页。"""
    if request.method == "HEAD":
        # FastAPI HEAD response - allow COOP checks from wallet SDKs
        return Response(status_code=200)
    return _tpl_response(request, "user.html")


@router.get("/trades", response_class=HTMLResponse)
async def trades_page(request: Request):
    """V2 交易记录页。"""
    return _tpl_response(request, "trades.html")


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Legacy admin entry is disabled on user-facing domain."""
    return Response(status_code=404)


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    """V2 功能管理页（积分 / 手续费 / 策略设定）。"""
    return _tpl_response(request, "config.html")


@router.get("/refer", response_class=HTMLResponse)
async def refer_page(request: Request):
    """V2 推荐好友页。"""
    return _tpl_response(request, "refer.html")


@router.get("/skeleton", response_class=HTMLResponse)
async def skeleton_page(request: Request):
    """V2 空白骨架页（模板参考）。"""
    return _tpl_response(request, "skeleton.html")


@router.get("/discover", response_class=HTMLResponse)
async def discover_page(request: Request):
    """T290: PM 事件发现列表页（predict.fun 风格）。T496: SSR 注入热缓存事件实现毫秒级首屏。

    T496+ Wave 5A: 把 ?p=<category> / ?t=<tag> 也纳入 SSR — 分类切换同样毫秒级。
    """
    ctx = _tpl_ctx(request)
    try:
        from scripts.dashboard_routes.discover_routes import (  # local import to avoid cycle
            get_ssr_payload,
            get_tags_payload,
            get_categories_payload,
        )

        qp = request.query_params
        p_arg = (qp.get("p") or "").strip().lower()
        t_arg = (qp.get("t") or "").strip().lower()
        tab_arg = (qp.get("tab") or "all").strip().lower() or "all"
        # T555 — read user lang preference from cookie so SSR titles paint
        # in the user's language on the very first frame (Discover used to
        # always serve English until the user manually switched language).
        lang_cookie = ""
        try:
            raw_lang = (request.cookies.get("veyra_lang") or "").strip()
            safe_lang = "".join(c for c in raw_lang if c.isalnum() or c in ("-", "_"))[
                :16
            ]
            if safe_lang and safe_lang.lower() != "en":
                lang_cookie = safe_lang
        except Exception:
            lang_cookie = ""
        ssr = get_ssr_payload(tab=tab_arg, limit=18, p=p_arg, t=t_arg, lang=lang_cookie)
        if ssr and isinstance(ssr.get("events"), list):
            ctx["ssr_events_json"] = json.dumps(ssr, ensure_ascii=False)
        else:
            ctx["ssr_events_json"] = ""
        # T508 Wave 7C: inline tags JSON when ?p= present so the subnav
        # paints in the very first render — no fetch round-trip.
        if p_arg:
            tags_payload = get_tags_payload(p_arg)
            ctx["ssr_tags_json"] = (
                json.dumps(tags_payload, ensure_ascii=False) if tags_payload else ""
            )
        else:
            ctx["ssr_tags_json"] = ""
        # T537 #537 — categories are virtually static; inline them in the
        # SSR payload so the top category chip nav paints in the first
        # frame on every visit (cold or warm), with zero network cost.
        try:
            cats_payload = get_categories_payload()
            ctx["ssr_cats_json"] = (
                json.dumps(cats_payload, ensure_ascii=False) if cats_payload else ""
            )
        except Exception:
            ctx["ssr_cats_json"] = ""
    except Exception:
        ctx["ssr_events_json"] = ""
        ctx["ssr_tags_json"] = ""
        ctx["ssr_cats_json"] = ""
    resp = templates.TemplateResponse(request, "discover.html", ctx)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    _apply_runtime_cookie(request, resp)
    return resp


@router.get("/event/{slug}", response_class=HTMLResponse)
async def event_detail_page(request: Request, slug: str):
    """T291: 事件详情 + 下单页（predict.fun 风格）。"""
    ctx = _tpl_ctx(request)
    ctx["event_slug"] = slug
    resp = templates.TemplateResponse(request, "event_detail.html", ctx)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    _apply_runtime_cookie(request, resp)
    return resp


@router.get("/discover/{slug}", response_class=HTMLResponse)
async def discover_detail_alias(request: Request, slug: str):
    """旧路径兼容：301 重定向到 /v2/event/{slug}。"""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url=f"/v2/event/{slug}", status_code=301)


@router.get("/deposit", response_class=HTMLResponse)
async def deposit_page(request: Request):
    """T289 Wave 5: USDC 充值指南页。"""
    return _tpl_response(request, "deposit.html")


@router.get("/withdraw", response_class=HTMLResponse)
async def withdraw_page(request: Request):
    """T460: USDC 提现页 — 从平台交易钱包提现至外部地址。"""
    return _tpl_response(request, "withdraw.html")


@router.get("/go", response_class=HTMLResponse)
async def go_help_page(request: Request):
    """T287: /go 指导手册页 —— 针对未完成 PM 钱包绑定的用户。
    内容由 /api/help-pages/go 返回的 Markdown 渲染；超级管理员可在管理后台可视化编辑。
    """
    return _help_tpl_response(request, "go")


@router.get("/faq", response_class=HTMLResponse)
async def faq_help_page(request: Request):
    return _help_tpl_response(request, "faq")


@router.get("/about", response_class=HTMLResponse)
async def about_help_page(request: Request):
    return _help_tpl_response(request, "about")


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_help_page(request: Request):
    return _help_tpl_response(request, "privacy")


@router.get("/terms", response_class=HTMLResponse)
async def terms_help_page(request: Request):
    return _help_tpl_response(request, "terms")


# ── T290 附录命名规范别名（无 /v2 前缀）────────────────────────────────
@alias_router.get("/discover", response_class=HTMLResponse)
async def alias_discover(request: Request):
    # T496: 与 /v2/discover 共享 SSR 注入路径，确保毫秒级首屏。
    return await discover_page(request)


@alias_router.get("/event/{slug}", response_class=HTMLResponse)
async def alias_event_detail(request: Request, slug: str):
    ctx = _tpl_ctx(request)
    ctx["event_slug"] = slug
    resp = templates.TemplateResponse(request, "event_detail.html", ctx)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    _apply_runtime_cookie(request, resp)
    return resp


@alias_router.get("/deposit")
async def alias_deposit(request: Request):
    """Legacy alias — permanently redirect to canonical /v2/deposit."""
    from fastapi.responses import RedirectResponse

    return RedirectResponse(url="/v2/deposit", status_code=301)


@router.get("/WeatherEventMap", response_class=HTMLResponse)
async def weather_event_map_page(request: Request):
    return _tpl_response(request, "weather_event_map.html")


@alias_router.get("/WeatherEventMap", response_class=HTMLResponse)
async def alias_weather_event_map(request: Request):
    return _tpl_response(request, "weather_event_map.html")


# T557 W1 — VeYra 体育赔率页（T558 重构后：去 PM/OddsPapi 化 + SSR + 内部
# 事件匹配过滤）。完整功能在 W2/W3 实现。
@router.get("/sportslive", response_class=HTMLResponse)
async def sportslive_page(request: Request):
    return _sportslive_response(request)


@alias_router.get("/sportslive", response_class=HTMLResponse)
async def alias_sportslive_page(request: Request):
    return _sportslive_response(request)


# T582 — 电竞专题页 /esports-live：复用 sportslive.html 渲染，但仅注入
# group=='esports' 的 sports，数据走同一套 OddsPapi warmer + PM tag 匹配。
@router.get("/esports-live", response_class=HTMLResponse)
async def esports_live_page(request: Request):
    return _sportslive_response(request, page_kind="esports")


@alias_router.get("/esports-live", response_class=HTMLResponse)
async def alias_esports_live_page(request: Request):
    return _sportslive_response(request, page_kind="esports")


def _sportslive_response(request: Request, page_kind: str = "sports") -> HTMLResponse:
    is_esports = page_kind == "esports"
    only_groups = {"esports"} if is_esports else None
    exclude_groups = None if is_esports else {"esports"}
    ctx = _tpl_ctx(request)
    qp = request.query_params
    # T561 G: 与 /discover?p=<slug> 体验一致 — 优先 ?p=，回退到 ?sport=（兼容历史）
    # T564 — 默认改为 'all' 聚合所有体育（warmer 已预热，零额外 IO）
    sport = (qp.get("p") or qp.get("sport") or "all").strip().lower()
    try:
        from scripts.dashboard_routes.sportslive_routes import (  # local import to avoid cycle
            get_ssr_events,
            _SUPPORTED_SPORTS,
            _aggregate_all_sports,
        )

        # T582 — 按页面归属过滤 sports 名单
        page_sports = [
            s
            for s in _SUPPORTED_SPORTS
            if (only_groups is None or s.get("group") in only_groups)
            and (exclude_groups is None or s.get("group") not in exclude_groups)
        ]
        valid_keys = {s["key"] for s in page_sports} | {"all"}
        if sport not in valid_keys:
            sport = "all"
        if sport == "all":
            ssr_payload = _aggregate_all_sports(
                2000,
                168,
                only_groups=only_groups,
                exclude_groups=exclude_groups,
            )
        else:
            ssr_payload = get_ssr_events(sport_key=sport, limit=200)
        ctx["ssr_sportslive_json"] = (
            json.dumps(ssr_payload, ensure_ascii=False) if ssr_payload else ""
        )
        ctx["ssr_sportslive_sport"] = sport
        ctx["ssr_sportslive_sports_json"] = json.dumps(
            [
                {
                    "key": s["key"],
                    "label_zh": s["label_zh"],
                    "label_en": s["label_en"],
                    "group": s["group"],
                }
                for s in page_sports
            ],
            ensure_ascii=False,
        )
        ctx["sportslive_page_kind"] = page_kind
    except Exception as e:
        ctx["ssr_sportslive_json"] = ""
        ctx["ssr_sportslive_sport"] = sport
        ctx["ssr_sportslive_sports_json"] = ""
        ctx["sportslive_page_kind"] = page_kind
        try:
            from scripts.dashboard_routes._shared import logger as _log

            _log.warning(f"[sportslive] SSR build failed: {e}")
        except Exception:
            pass
    # T561 W5-D: 注入 admin 可配置的 AI banner 文案 / 启用状态
    try:
        from scripts.dashboard_routes.frontend_banner_routes import (
            get_banner,
            list_banners_for_page,
        )

        banner = get_banner("sportslive_ai") or {}
        # T582：esports 页不复用 /sportslive 的 AI banner（避免 hero 标题被覆盖）
        if is_esports:
            ctx["sportslive_ai_banner_json"] = "null"
        else:
            ctx["sportslive_ai_banner_json"] = json.dumps(banner, ensure_ascii=False)
        # T583 — 页面级 banners（含 image_url / link_url）供跨页互链使用
        page_path = "/esports-live" if is_esports else "/sportslive"
        ctx["sportslive_page_banners_json"] = json.dumps(
            list_banners_for_page(page_path) or [], ensure_ascii=False
        )
        ctx["sportslive_page_path"] = page_path
    except Exception:
        ctx["sportslive_ai_banner_json"] = ""
        ctx["sportslive_page_banners_json"] = "[]"
        ctx["sportslive_page_path"] = "/esports-live" if is_esports else "/sportslive"
    resp = templates.TemplateResponse(request, "sportslive.html", ctx)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    _apply_runtime_cookie(request, resp)
    return resp


@router.get("/Copytrader", response_class=HTMLResponse)
async def copytrader_v2_page(request: Request):
    return _tpl_response(request, "Copytrader.html")


@alias_router.get("/Copytrader", response_class=HTMLResponse)
async def alias_copytrader_v2(request: Request):
    return _tpl_response(request, "Copytrader.html")


# T610 — /leaderboard 排行榜页面（V1 mock 数据）
@router.get("/leaderboard", response_class=HTMLResponse)
async def leaderboard_page(request: Request):
    return _tpl_response(request, "leaderboard.html")


@alias_router.get("/leaderboard", response_class=HTMLResponse)
async def alias_leaderboard_page(request: Request):
    return _tpl_response(request, "leaderboard.html")


@router.get("/valuelab", response_class=HTMLResponse)
async def valuelab_page(request: Request):
    """T735/T743: Value Lab 列表页采用骨架优先首屏，数据由前端渐进加载。"""
    ctx = _tpl_ctx(request)
    ctx["ssr_valuelab_json"] = ""
    ctx["valuelab_page_banners_json"] = "[]"
    ctx["valuelab_page_path"] = "/valuelab"
    try:
        from scripts.dashboard_routes.valuelab_routes import get_ssr_valuelab_payload

        tab_arg = (request.query_params.get("tab") or "crypto").strip().lower()
        lang_cookie = str(request.cookies.get("veyra_lang") or "en")
        ssr = await asyncio.wait_for(
            asyncio.to_thread(
                get_ssr_valuelab_payload,
                tab_arg,
                18,
                lang_cookie,
            ),
            timeout=0.55,
        )
        if ssr and isinstance(ssr.get("items"), list):
            ctx["ssr_valuelab_json"] = json.dumps(ssr, ensure_ascii=False)
    except Exception:
        ctx["ssr_valuelab_json"] = ""

    try:
        from scripts.dashboard_routes.frontend_banner_routes import (
            list_banners_for_page,
        )

        ctx["valuelab_page_banners_json"] = json.dumps(
            list_banners_for_page("/valuelab") or [], ensure_ascii=False
        )
        ctx["valuelab_page_path"] = "/valuelab"
    except Exception:
        ctx["valuelab_page_banners_json"] = "[]"
        ctx["valuelab_page_path"] = "/valuelab"

    resp = templates.TemplateResponse(request, "valuelab.html", ctx)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    _apply_runtime_cookie(request, resp)
    return resp


@alias_router.get("/valuelab", response_class=HTMLResponse)
async def alias_valuelab_page(request: Request):
    return await valuelab_page(request)


@router.get("/valuelab/{slug}", response_class=HTMLResponse)
async def valuelab_detail_page(request: Request, slug: str):
    """T735: Value Lab 详情页（Terminal 风格分析日志）。"""
    ctx = _tpl_ctx(request)
    ctx["valuelab_slug"] = slug
    try:
        from scripts.dashboard_routes.valuelab_routes import (
            get_valuelab_analysis_payload,
        )

        detail = await asyncio.wait_for(
            asyncio.to_thread(
                get_valuelab_analysis_payload,
                slug,
                str(request.cookies.get("veyra_lang") or "en"),
            ),
            timeout=0.55,
        )
        ctx["ssr_valuelab_detail_json"] = (
            json.dumps(detail, ensure_ascii=False) if detail else ""
        )
    except Exception:
        ctx["ssr_valuelab_detail_json"] = ""
    resp = templates.TemplateResponse(request, "valuelab_detail.html", ctx)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    _apply_runtime_cookie(request, resp)
    return resp


@alias_router.get("/valuelab/{slug}", response_class=HTMLResponse)
async def alias_valuelab_detail_page(request: Request, slug: str):
    return await valuelab_detail_page(request, slug)


# T476: Guest preview page — accessible without login. Linked from index CTA right-side image.
_GUEST_PREVIEW_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="index,follow">
<title>VeYra · Guest Preview</title>
<style>
  html,body{margin:0;padding:0;background:#0b1220;color:#e6f1ec;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh}
  .wrap{max-width:920px;margin:0 auto;padding:48px 24px;text-align:center}
  h1{font-size:34px;font-weight:800;letter-spacing:-.5px;margin:18px 0 10px;background:linear-gradient(90deg,#2ee68b,#9bf6ca);-webkit-background-clip:text;color:transparent}
  p{color:#8ba09a;font-size:15px;line-height:1.6;margin:8px 0}
  .badge{display:inline-block;padding:6px 12px;border:1px solid rgba(46,230,139,.3);border-radius:999px;font-size:12px;color:#9bf6ca;margin-bottom:18px}
  .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin:30px 0}
  .card{background:rgba(46,230,139,.06);border:1px solid rgba(46,230,139,.18);border-radius:14px;padding:18px}
  .card h3{margin:0 0 6px;font-size:15px;color:#d8ffec}
  .card p{margin:0;font-size:13px}
  a.cta{display:inline-block;margin-top:12px;padding:12px 22px;background:linear-gradient(90deg,#2ee68b,#9bf6ca);color:#032115;border-radius:10px;text-decoration:none;font-weight:800;font-size:14px}
</style></head><body>
<div class="wrap">
  <span class="badge">PREVIEW · COMING SOON</span>
  <h1>VeYra Guest Preview</h1>
  <p>An unrestricted, no-login window into our prediction-market intelligence layer.</p>
  <p>Browse live markets, AI signals and execution flow — no wallet required.</p>
  <div class="grid">
    <div class="card"><h3>📊 Live Markets</h3><p>Polymarket order books, prices and volume.</p></div>
    <div class="card"><h3>🧠 AI Signals</h3><p>Multi-model edge detection and risk scores.</p></div>
    <div class="card"><h3>⚡ Auto-Trader</h3><p>Gasless, hardware-secured execution layer.</p></div>
    <div class="card"><h3>🌍 Smart Money</h3><p>Top-trader leaderboards and copy-trade signals.</p></div>
  </div>
  <a class="cta" href="/">Back to Login →</a>
</div>
</body></html>"""


@alias_router.get("/preview", response_class=HTMLResponse)
async def preview_guest_page(request: Request):
    """Public preview page rendered in V2 shell with native Seaz bundle."""
    ctx = _tpl_ctx(request)
    seaz_entry_js, seaz_entry_css = _resolve_seaz_entry_assets()
    ctx["seaz_entry_js"] = seaz_entry_js
    ctx["seaz_entry_css"] = seaz_entry_css
    try:
        return templates.TemplateResponse(request, "preview.html", ctx)
    except Exception:
        resp = HTMLResponse(content=_GUEST_PREVIEW_HTML)
        resp.headers["Cache-Control"] = "public, max-age=60"
        return resp


@alias_router.get("/Builders", response_class=HTMLResponse)
@alias_router.get("/builders", response_class=HTMLResponse)
async def builders_guest_page(request: Request):
    """Public Builders page cloned from /preview for independent evolution."""
    ctx = _tpl_ctx(request)
    period = (request.query_params.get("timePeriod") or "DAY").upper()
    if period not in ("DAY", "WEEK", "MONTH", "ALL"):
        period = "DAY"
    try:
        from scripts.dashboard_routes.ecosystem_routes import (
            get_public_builders_payload,
        )

        payload = get_public_builders_payload(period)
        ctx["ssr_builders_json"] = json.dumps(payload, ensure_ascii=False)
    except Exception:
        ctx["ssr_builders_json"] = ""
    ctx["builders_period"] = period
    try:
        return templates.TemplateResponse(request, "builders.html", ctx)
    except Exception:
        resp = HTMLResponse(content=_GUEST_PREVIEW_HTML)
        resp.headers["Cache-Control"] = "public, max-age=60"
        return resp
