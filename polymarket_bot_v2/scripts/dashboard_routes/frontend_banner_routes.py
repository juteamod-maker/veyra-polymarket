"""T561 W5-D — Frontend Banner 管理路由。

支持管理员维护“前台横幅”文案 / 启用状态，前端页面通过
`/api/public/frontend-banner/<slot>` 拉取。当前接入的 slot：
- `sportslive_ai`  → /sportslive 页 AI 提示横幅

存储：单一 JSON 文件 `config/frontend_banners.json`，无需新增 DB 表。
管理员鉴权统一走 `_shared.get_request_identity` 复用与 menu/homepage 一致的 admin 边界。
"""

from __future__ import annotations

import json
import os
import time
from threading import Lock
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from scripts.dashboard_routes._shared import get_request_identity, logger

router = APIRouter()

_BANNER_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
    "frontend_banners.json",
)
_lock = Lock()
_cache: dict[str, Any] = {}
_cache_ts = 0.0
# T591 — 把缓存 TTL 收紧到 1s，确保后台修改在最多 1 秒内被前端 SSR / 公开
# 接口读到。文件极小（<5KB），无 IO 性能压力。
_CACHE_TTL = 1.0
# T591 — 系统 slot 也可以被 admin 删除（写入 tombstone），删除后前端不再渲染。
# 用 `_TOMBSTONE_KEY` 在磁盘 JSON 中维护被删除的系统 slot 列表，admin reset 会清除 tombstone。
_TOMBSTONE_KEY = "__deleted_system_slots__"

# T584 — Banner page-slot 字段：banner 可以归属到一个或多个前台页面。
# 内置可选页面 — 与前台路由保持一致；"all" 表示在任何可用页面都显示。
_AVAILABLE_PAGES: tuple[str, ...] = (
    "/",
    "/discover",
    "/valuelab",
    "/sportslive",
    "/esports-live",
    "/leaderboard",
    "/copytrader",
)

# T598 — placement 决定 banner 在页面上的渲染位置：
#   sidebar_image  — 顶部右侧的图片横幅条（需 image_url），多张并排
#   hero_text      — 覆盖页面 hero 区域（h1/p）的纯文案横幅，不需要 image_url
# 旧记录无 placement 字段时按 image_url 是否存在自动判定（兼容回退）。
_PLACEMENT_VALUES: tuple[str, ...] = ("sidebar_image", "hero_text")

_DEFAULT_BANNERS: dict[str, dict[str, Any]] = {
    "sportslive_ai": {
        "enabled": True,
        # T599 — i18n_key 优先：前端先查 tCore(title_i18n_key)，命中即使用 admin
        # 在 zh-CN/en/fr/ja/... 任一语种维护的译文（来自动态字典 /api/i18n/dynamic）。
        # 无 i18n_key 时回退到下方 title_<lang> 兜底。
        "title_i18n_key": "sportslive.title",
        "subtitle_i18n_key": "sportslive.banner.ai.subtitle",
        "label_i18n_key": "",
        "title_en": "VeYra AI",
        "title_zh": "VeYra AI",
        "subtitle_en": "Tap to highlight matches with cross-book betting value.",
        "subtitle_zh": "点击仅显示有跨博彩价值的赛事。",
        "label_en": "Sportslive · AI Banner",
        "label_zh": "赛事赔率 · AI 横幅",
        "hint_en": "Top banner shown on /sportslive between status tabs and matches.",
        "hint_zh": "显示在 /sportslive 状态 Tab 与赛事卡片之间的顶部横幅。",
        "pages": ["/sportslive"],
        "image_url": "",
        "link_url": "",
        "placement": "hero_text",
    },
}

# T568 — system slots cannot be deleted; user-created slots live alongside.
_SYSTEM_SLOTS = frozenset(_DEFAULT_BANNERS.keys())
# T584 — `pages` (list[str]) / `image_url` / `link_url` 加入 schema。
_ALLOWED_FIELDS = (
    "enabled",
    # T599 — i18n_key 是目标架构主字段；label_*/title_*/subtitle_* 仅作向下兼容
    # 兜底（admin 仅维护 i18n_key + label_en 即可，其它语种由动态字典覆盖）。
    "title_i18n_key",
    "subtitle_i18n_key",
    "label_i18n_key",
    "title_en",
    "title_zh",
    "subtitle_en",
    "subtitle_zh",
    "label_en",
    "label_zh",
    "hint_en",
    "hint_zh",
    "pages",
    "image_url",
    "link_url",
    "placement",  # T598
)
_LIST_FIELDS = frozenset({"pages"})

import re as _re

_SLOT_KEY_RE = _re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def _load_tombstones() -> set[str]:
    on_disk = _load_disk()
    raw = on_disk.get(_TOMBSTONE_KEY) or []
    if isinstance(raw, list):
        return {str(s) for s in raw if isinstance(s, str)}
    return set()


def _all_slot_keys() -> set[str]:
    """默认 + 磁盘上已创建的动态 slot 全集（含被 tombstone 的系统 slot，便于 reset 校验）。"""
    on_disk = _load_disk()
    return set(_DEFAULT_BANNERS.keys()) | {
        k
        for k in (on_disk or {}).keys()
        if isinstance(k, str) and _SLOT_KEY_RE.match(k)
    }


def _is_admin(request: Request) -> bool:
    ident = get_request_identity(request) or {}
    return bool(ident.get("authenticated") and ident.get("is_admin"))


def _load_disk() -> dict[str, Any]:
    try:
        if not os.path.exists(_BANNER_FILE):
            return {}
        with open(_BANNER_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        logger.warning(f"[frontend-banner] load failed: {exc}")
    return {}


def _save_disk(data: dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(_BANNER_FILE), exist_ok=True)
    tmp = _BANNER_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, _BANNER_FILE)


def _merged() -> dict[str, dict[str, Any]]:
    """带默认值合并 — 缺失 slot/字段都回落到 default；动态 slot 以磁盘为准。"""
    global _cache, _cache_ts
    now = time.time()
    if now - _cache_ts < _CACHE_TTL and _cache:
        return _cache
    with _lock:
        if now - _cache_ts < _CACHE_TTL and _cache:
            return _cache
        on_disk = _load_disk()
        tombstones = _load_tombstones()
        merged: dict[str, dict[str, Any]] = {}
        # 1) seed system slots with defaults, then overlay disk overrides
        for slot, defaults in _DEFAULT_BANNERS.items():
            if slot in tombstones:
                # T591 — admin 已删除此系统 slot，前端不再渲染；reset 后会自动恢复。
                continue
            base: dict[str, Any] = {k: defaults.get(k) for k in _ALLOWED_FIELDS}
            override = on_disk.get(slot) or {}
            if isinstance(override, dict):
                for k in _ALLOWED_FIELDS:
                    if k in override:
                        base[k] = override[k]
            base["is_system"] = True
            base = _normalize_banner(base)
            merged[slot] = base
        # 2) attach dynamic (user-created) slots
        for slot, payload in (on_disk or {}).items():
            if slot == _TOMBSTONE_KEY:
                continue
            if slot in merged:
                continue
            if not (isinstance(slot, str) and _SLOT_KEY_RE.match(slot)):
                continue
            if not isinstance(payload, dict):
                continue
            base = {k: payload.get(k) for k in _ALLOWED_FIELDS}
            if base.get("enabled") is None:
                base["enabled"] = False
            base["is_system"] = False
            merged[slot] = _normalize_banner(base)
        _cache = merged
        _cache_ts = now
        return merged


def _sanitize_pages(value: Any) -> list[str]:
    """将任意输入规范化为合法的 page 列表。

    - 接受 list[str]、逗号分隔字符串。
    - 仅保留 `_AVAILABLE_PAGES` 白名单中的项；保留特殊值 `all`。
    - 去重后保持声明顺序。
    """
    if value is None:
        return []
    raw: list[Any]
    if isinstance(value, str):
        raw = [p.strip() for p in value.split(",")]
    elif isinstance(value, (list, tuple)):
        raw = list(value)
    else:
        return []
    allowed = set(_AVAILABLE_PAGES) | {"all"}
    seen: set[str] = set()
    out: list[str] = []
    for p in raw:
        if not isinstance(p, str):
            continue
        s = p.strip()
        if not s:
            continue
        if s in allowed and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _normalize_banner(payload: dict[str, Any]) -> dict[str, Any]:
    """应用类型校正：pages 列表化、image_url/link_url 字符串化、placement 校验。"""
    out = dict(payload)
    out["pages"] = _sanitize_pages(out.get("pages"))
    out["image_url"] = (
        "" if out.get("image_url") is None else str(out.get("image_url") or "")
    )[:500]
    out["link_url"] = (
        "" if out.get("link_url") is None else str(out.get("link_url") or "")
    )[:500]
    # T598 — placement 默认按 image_url 判定（向后兼容旧记录）
    raw_pl = (out.get("placement") or "").strip()
    if raw_pl not in _PLACEMENT_VALUES:
        raw_pl = (
            "sidebar_image" if (out.get("image_url") or "").strip() else "hero_text"
        )
    out["placement"] = raw_pl
    return out


def get_banner(slot: str) -> dict[str, Any] | None:
    """供其它模块（如 SSR）直接读取。"""
    if not (isinstance(slot, str) and _SLOT_KEY_RE.match(slot)):
        return None
    merged = _merged()
    if slot not in merged:
        return None
    return dict(merged.get(slot) or {})


def list_banners_for_page(
    page: str, *, placement: str | None = None
) -> list[dict[str, Any]]:
    """返回页面的 enabled banner（按 placement 过滤）。

    T598 行为：
    - 默认（placement=None）→ 返回该页面所有 enabled banner（不再因缺 image_url 而吞掉文案 banner）。
    - placement='sidebar_image' → 只返回带 image_url 的图片 banner。
    - placement='hero_text'     → 只返回 hero 文案 banner（覆盖 h1/p）。
    """
    page_norm = (page or "").strip()
    if not page_norm:
        return []
    merged = _merged()
    out: list[dict[str, Any]] = []
    for slot, b in merged.items():
        if not b or not b.get("enabled"):
            continue
        pages = b.get("pages") or []
        if not isinstance(pages, list):
            pages = []
        if not pages:
            continue
        if not ("all" in pages or page_norm in pages):
            continue
        pl = b.get("placement") or "sidebar_image"
        if placement is not None and pl != placement:
            continue
        if placement == "sidebar_image" and not (b.get("image_url") or "").strip():
            continue
        entry = dict(b)
        entry["slot"] = slot
        out.append(entry)
    return out


# ── Public read ────────────────────────────────
# T584 — 公共接口：返回所有可用页面 slug，给前端做下拉选项。
# 注意：必须注册在 /{slot} 之前，否则会被通配 slot 路由吞掉返回 404。
@router.get("/api/public/frontend-banner/_pages")
async def public_available_pages() -> JSONResponse:
    return JSONResponse({"ok": True, "pages": list(_AVAILABLE_PAGES)})


# T584 — 公共接口：按页面拉取所有归属此页且 enabled 的 banners（含 image_url / link_url）。
@router.get("/api/public/frontend-banner/page/{page_key:path}")
async def public_list_for_page(page_key: str) -> JSONResponse:
    page_norm = ("/" + page_key.lstrip("/")).rstrip("/") if page_key else ""
    # 兼容首页 "/"：strip 后会变空字符串
    if page_norm == "":
        page_norm = "/"
    items = list_banners_for_page(page_norm)
    return JSONResponse(
        {"ok": True, "page": page_norm, "count": len(items), "banners": items}
    )


@router.get("/api/public/frontend-banner/{slot}")
async def public_get(slot: str) -> JSONResponse:
    if not (isinstance(slot, str) and _SLOT_KEY_RE.match(slot)):
        return JSONResponse({"ok": False, "error": "unknown_slot"}, status_code=404)
    merged = _merged()
    if slot not in merged:
        return JSONResponse({"ok": False, "error": "unknown_slot"}, status_code=404)
    return JSONResponse({"ok": True, "slot": slot, "banner": dict(merged[slot])})


# ── Admin ────────────────────────────────────────────────
@router.get("/api/admin/frontend-banner/list")
async def admin_list(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    return JSONResponse({"ok": True, "banners": _merged()})


@router.post("/api/admin/frontend-banner/update")
async def admin_update(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    slot = str(body.get("slot") or "").strip()
    if not (slot and _SLOT_KEY_RE.match(slot)):
        return JSONResponse({"ok": False, "error": "unknown_slot"}, status_code=400)
    if slot not in _all_slot_keys():
        return JSONResponse({"ok": False, "error": "unknown_slot"}, status_code=400)
    patch: dict[str, Any] = {}
    for k in _ALLOWED_FIELDS:
        if k in body:
            v = body[k]
            if k == "enabled":
                patch[k] = bool(v)
            elif k in _LIST_FIELDS:
                patch[k] = _sanitize_pages(v)
            elif k in ("image_url", "link_url"):
                patch[k] = ("" if v is None else str(v))[:500]
            else:
                patch[k] = ("" if v is None else str(v))[:280]
    if not patch:
        return JSONResponse({"ok": False, "error": "no_fields"}, status_code=400)
    with _lock:
        on_disk = _load_disk()
        cur = dict(on_disk.get(slot) or {})
        cur.update(patch)
        on_disk[slot] = cur
        try:
            _save_disk(on_disk)
        except Exception as exc:
            logger.warning(f"[frontend-banner] save failed: {exc}")
            return JSONResponse({"ok": False, "error": "save_failed"}, status_code=500)
        # 失效内存缓存
        global _cache_ts
        _cache_ts = 0.0
    logger.info(f"[frontend-banner] slot={slot} updated fields={list(patch.keys())}")
    return JSONResponse({"ok": True, "slot": slot, "banner": get_banner(slot) or {}})


@router.post("/api/admin/frontend-banner/reset")
async def admin_reset(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    slot = str(body.get("slot") or "").strip()
    if slot not in _SYSTEM_SLOTS:
        return JSONResponse({"ok": False, "error": "unknown_slot"}, status_code=400)
    with _lock:
        on_disk = _load_disk()
        on_disk.pop(slot, None)
        # T591 — 同时清除 tombstone，使 reset 真正恢复默认显示。
        tomb_raw = on_disk.get(_TOMBSTONE_KEY) or []
        if isinstance(tomb_raw, list) and slot in tomb_raw:
            on_disk[_TOMBSTONE_KEY] = [s for s in tomb_raw if s != slot]
            if not on_disk[_TOMBSTONE_KEY]:
                on_disk.pop(_TOMBSTONE_KEY, None)
        try:
            _save_disk(on_disk)
        except Exception as exc:
            logger.warning(f"[frontend-banner] reset save failed: {exc}")
            return JSONResponse({"ok": False, "error": "save_failed"}, status_code=500)
        global _cache_ts
        _cache_ts = 0.0
    logger.info(f"[frontend-banner] slot={slot} reset to default")
    return JSONResponse({"ok": True, "slot": slot, "banner": get_banner(slot) or {}})


# T568 — full CRUD: create / delete dynamic slots ----------------------------
@router.post("/api/admin/frontend-banner/create")
async def admin_create(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    slot = str(body.get("slot") or "").strip().lower()
    if not (slot and _SLOT_KEY_RE.match(slot)):
        return JSONResponse({"ok": False, "error": "invalid_slot_key"}, status_code=400)
    if slot in _all_slot_keys():
        return JSONResponse({"ok": False, "error": "slot_exists"}, status_code=409)
    new_payload: dict[str, Any] = {"enabled": bool(body.get("enabled", False))}
    for k in _ALLOWED_FIELDS:
        if k == "enabled":
            continue
        v = body.get(k)
        if k in _LIST_FIELDS:
            new_payload[k] = _sanitize_pages(v)
        elif k in ("image_url", "link_url"):
            new_payload[k] = ("" if v is None else str(v))[:500]
        else:
            new_payload[k] = ("" if v is None else str(v))[:280]
    with _lock:
        on_disk = _load_disk()
        on_disk[slot] = new_payload
        try:
            _save_disk(on_disk)
        except Exception as exc:
            logger.warning(f"[frontend-banner] create save failed: {exc}")
            return JSONResponse({"ok": False, "error": "save_failed"}, status_code=500)
        global _cache_ts
        _cache_ts = 0.0
    logger.info(f"[frontend-banner] slot={slot} created (dynamic)")
    return JSONResponse({"ok": True, "slot": slot, "banner": get_banner(slot) or {}})


_SIDEBAR_PROMO_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config",
    "sidebar_promo.json",
)
_SIDEBAR_PROMO_DEFAULTS: dict[str, str] = {
    "image_url": "/static/VeyraPLUNDER.png",
    "link_url": "https://veyra.trade/preview",
}
_sp_lock = Lock()


def _load_sidebar_promo() -> dict[str, str]:
    try:
        if os.path.exists(_SIDEBAR_PROMO_FILE):
            with open(_SIDEBAR_PROMO_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {
                    "image_url": str(data.get("image_url") or _SIDEBAR_PROMO_DEFAULTS["image_url"])[:500],
                    "link_url": str(data.get("link_url") or _SIDEBAR_PROMO_DEFAULTS["link_url"])[:500],
                }
    except Exception as exc:
        logger.warning(f"[sidebar-promo] load failed: {exc}")
    return dict(_SIDEBAR_PROMO_DEFAULTS)


def _save_sidebar_promo(data: dict[str, str]) -> None:
    os.makedirs(os.path.dirname(_SIDEBAR_PROMO_FILE), exist_ok=True)
    tmp = _SIDEBAR_PROMO_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _SIDEBAR_PROMO_FILE)


@router.get("/api/public/sidebar-promo")
def public_sidebar_promo() -> JSONResponse:
    return JSONResponse(_load_sidebar_promo())


@router.post("/api/admin/sidebar-promo")
async def admin_update_sidebar_promo(request: Request) -> JSONResponse:
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    image_url = str(body.get("image_url") or "").strip()[:500]
    link_url = str(body.get("link_url") or "").strip()[:500]
    if not image_url:
        image_url = _SIDEBAR_PROMO_DEFAULTS["image_url"]
    if not link_url:
        link_url = _SIDEBAR_PROMO_DEFAULTS["link_url"]
    with _sp_lock:
        try:
            _save_sidebar_promo({"image_url": image_url, "link_url": link_url})
        except Exception as exc:
            logger.warning(f"[sidebar-promo] save failed: {exc}")
            return JSONResponse({"ok": False, "error": "save_failed"}, status_code=500)
    logger.info(f"[sidebar-promo] updated image_url={image_url} link_url={link_url}")
    return JSONResponse({"ok": True, "image_url": image_url, "link_url": link_url})


@router.post("/api/admin/frontend-banner/delete")
async def admin_delete(request: Request) -> JSONResponse:
    global _cache_ts
    if not _is_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    slot = str(body.get("slot") or "").strip()
    if not (slot and _SLOT_KEY_RE.match(slot)):
        return JSONResponse({"ok": False, "error": "unknown_slot"}, status_code=400)
    with _lock:
        on_disk = _load_disk()
        if slot in _SYSTEM_SLOTS:
            # T591 — 系统 slot 走 tombstone 删除（保留 reset 路径）。
            tomb_raw = on_disk.get(_TOMBSTONE_KEY) or []
            if not isinstance(tomb_raw, list):
                tomb_raw = []
            tomb_set = {str(s) for s in tomb_raw if isinstance(s, str)}
            tomb_set.add(slot)
            on_disk[_TOMBSTONE_KEY] = sorted(tomb_set)
            # 同时清除可能存在的 override，避免 reset 时还残留旧值
            on_disk.pop(slot, None)
            try:
                _save_disk(on_disk)
            except Exception as exc:
                logger.warning(f"[frontend-banner] system delete save failed: {exc}")
                return JSONResponse(
                    {"ok": False, "error": "save_failed"}, status_code=500
                )
            _cache_ts = 0.0
            logger.info(f"[frontend-banner] system slot={slot} tombstoned")
            return JSONResponse({"ok": True, "slot": slot, "system": True})
        if slot not in on_disk:
            return JSONResponse({"ok": False, "error": "unknown_slot"}, status_code=404)
        on_disk.pop(slot, None)
        try:
            _save_disk(on_disk)
        except Exception as exc:
            logger.warning(f"[frontend-banner] delete save failed: {exc}")
            return JSONResponse({"ok": False, "error": "save_failed"}, status_code=500)
        _cache_ts = 0.0
    logger.info(f"[frontend-banner] slot={slot} deleted (dynamic)")
    return JSONResponse({"ok": True, "slot": slot})
