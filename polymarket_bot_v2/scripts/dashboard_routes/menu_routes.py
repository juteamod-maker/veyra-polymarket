"""
dashboard_routes/menu_routes.py — T302 应用菜单管理

端点:
  - GET  /api/menu/list           当前用户可见菜单（按 role 过滤）
  - GET  /api/menu/admin/list     全部菜单（admin 专用）
  - POST /api/menu/admin/create   新建菜单
  - POST /api/menu/admin/update   更新菜单
  - POST /api/menu/admin/delete   删除菜单
  - POST /api/menu/admin/reorder  批量重排序

RBAC: admin/super_admin 可管理全部；普通用户仅能 GET /api/menu/list（按 role_required 过滤）
"""

import asyncio
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from scripts.dashboard_routes._shared import (
    db_connect,
    get_request_identity,
    logger,
)
from scripts.dashboard_routes.frontend_routes import invalidate_nav_menu_cache

router = APIRouter()


# ── 内部工具 ──────────────────────────────────────────────
def _row_to_menu(row: Any) -> dict:
    """app_menus 行 → dict（兼容 RealDictCursor dict 与 tuple）"""
    if row is None:
        return {}
    if isinstance(row, dict):
        return {
            "id": row.get("id"),
            "label": row.get("label"),
            "path": row.get("path"),
            "icon": row.get("icon") or "circle",
            "sort_order": int(row.get("sort_order") or 0),
            "visible": bool(row.get("visible")),
            "role_required": row.get("role_required") or "user",
            "section_label": row.get("section_label"),
            # T558 — i18n 字段。三者任一可空，前端会按优先级选择：
            # i18n_key → label_<lang> → label。
            "i18n_key": row.get("i18n_key") or "",
            "label_zh": row.get("label_zh") or "",
            "label_en": row.get("label_en") or "",
        }
    return {
        "id": row[0],
        "label": row[1],
        "path": row[2],
        "icon": row[3] or "circle",
        "sort_order": int(row[4] or 0),
        "visible": bool(row[5]),
        "role_required": row[6] or "user",
        "section_label": row[7],
        "i18n_key": (row[8] if len(row) > 8 else None) or "",
        "label_zh": (row[9] if len(row) > 9 else None) or "",
        "label_en": (row[10] if len(row) > 10 else None) or "",
    }


def _require_admin(request: Request) -> dict | None:
    ident = get_request_identity(request)
    if not ident or not ident.get("is_admin"):
        return None
    return ident


# ── GET /api/menu/list ─────────────────────────────────────
@router.get("/api/menu/list")
async def list_menu_for_user(request: Request):
    """返回当前用户可见的菜单（按 role_required 过滤 + visible=TRUE）"""
    ident = get_request_identity(request)
    role = (ident or {}).get("role") or "guest"
    is_admin = bool((ident or {}).get("is_admin"))

    def _fetch():
        with db_connect() as conn:
            cursor = conn.execute(
                "SELECT id,label,path,icon,sort_order,visible,role_required,section_label,"
                "i18n_key,label_zh,label_en "
                "FROM app_menus WHERE visible = TRUE ORDER BY sort_order ASC, id ASC"
            )
            return cursor.fetchall()

    try:
        # T565b — sync DB inside async handler blocks event loop. Run in threadpool.
        rows = await asyncio.to_thread(_fetch)
        items = []
        for r in rows:
            m = _row_to_menu(r)
            req_role = (m.get("role_required") or "user").lower()
            # admin/super_admin 看全部；普通用户只看 role_required = 'user'
            if req_role == "admin" and not is_admin:
                continue
            items.append(m)
        return JSONResponse(
            {"ok": True, "items": items, "role": role, "is_admin": is_admin},
            headers={
                # T603C — 实时菜单：admin 改完后任何端立即看到，禁缓存。
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Vary": "Cookie",
            },
        )
    except Exception as e:
        logger.warning(f"[menu/list] {e}")
        # 失败时返回空数组（base.html 硬编码兜底）
        return JSONResponse({"ok": False, "items": [], "error": str(e)})


# ── GET /api/menu/admin/list ───────────────────────────────
@router.get("/api/menu/admin/list")
async def list_menu_admin(request: Request):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)

    def _fetch():
        with db_connect() as conn:
            cursor = conn.execute(
                "SELECT id,label,path,icon,sort_order,visible,role_required,section_label,"
                "i18n_key,label_zh,label_en "
                "FROM app_menus ORDER BY sort_order ASC, id ASC"
            )
            return cursor.fetchall()

    try:
        rows = await asyncio.to_thread(_fetch)
        # T601 — admin 列表绝不可被 CDN/浏览器缓存，否则 admin 改动后看到旧值
        return JSONResponse(
            {"ok": True, "items": [_row_to_menu(r) for r in rows]},
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )
    except Exception as e:
        logger.warning(f"[menu/admin/list] {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── POST /api/menu/admin/create ────────────────────────────
@router.post("/api/menu/admin/create")
async def create_menu(request: Request):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    label = (body.get("label") or "").strip()
    path = (body.get("path") or "").strip()
    icon = (body.get("icon") or "circle").strip()
    sort_order = int(body.get("sort_order") or 100)
    visible = bool(body.get("visible", True))
    role_required = (body.get("role_required") or "user").strip().lower()
    section_label = (body.get("section_label") or "").strip() or None
    # T558 — 可选 i18n 字段，空串存 NULL 避免误触发 i18n 查找。
    i18n_key = (body.get("i18n_key") or "").strip() or None
    label_zh = (body.get("label_zh") or "").strip() or None
    label_en = (body.get("label_en") or "").strip() or None

    if not label or not path:
        return JSONResponse(
            {"ok": False, "error": "label_and_path_required"}, status_code=400
        )
    if role_required not in ("user", "admin"):
        return JSONResponse({"ok": False, "error": "invalid_role"}, status_code=400)
    if not path.startswith("/"):
        return JSONResponse(
            {"ok": False, "error": "path_must_start_with_slash"}, status_code=400
        )

    def _do():
        with db_connect() as conn:
            conn.execute(
                "INSERT INTO app_menus (label,path,icon,sort_order,visible,role_required,section_label,"
                "i18n_key,label_zh,label_en) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    label,
                    path,
                    icon,
                    sort_order,
                    visible,
                    role_required,
                    section_label,
                    i18n_key,
                    label_zh,
                    label_en,
                ),
            )

    try:
        await asyncio.to_thread(_do)
        invalidate_nav_menu_cache()
        logger.info(f"[menu/create] {label} -> {path}")
        return JSONResponse(
            {"ok": True},
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        msg = str(e).lower()
        if "unique" in msg or "duplicate" in msg:
            return JSONResponse(
                {"ok": False, "error": "path_already_exists"}, status_code=409
            )
        logger.warning(f"[menu/create] {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── POST /api/menu/admin/update ────────────────────────────
@router.post("/api/menu/admin/update")
async def update_menu(request: Request):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)

    mid = body.get("id")
    if not mid:
        return JSONResponse({"ok": False, "error": "id_required"}, status_code=400)

    fields: list[str] = []
    values: list[Any] = []
    allowed = {
        "label": str,
        "path": str,
        "icon": str,
        "sort_order": int,
        "visible": bool,
        "role_required": str,
        "section_label": str,
        # T558 — 三个 i18n 字段与 section_label 同语义：空串 → NULL
        "i18n_key": str,
        "label_zh": str,
        "label_en": str,
    }
    for k, caster in allowed.items():
        if k in body:
            v = body[k]
            if v is None and k in ("section_label", "i18n_key", "label_zh", "label_en"):
                fields.append(f"{k} = %s")
                values.append(None)
                continue
            try:
                v = caster(v) if caster is not bool else bool(v)
            except Exception:
                continue
            if k == "role_required" and v not in ("user", "admin"):
                continue
            if k == "path" and (not v or not v.startswith("/")):
                continue
            # 空串也记为 NULL，避免误触发前端 i18n 查找
            if (
                k in ("i18n_key", "label_zh", "label_en", "section_label")
                and isinstance(v, str)
                and not v.strip()
            ):
                fields.append(f"{k} = %s")
                values.append(None)
                continue
            fields.append(f"{k} = %s")
            values.append(v)
    if not fields:
        return JSONResponse({"ok": False, "error": "no_fields"}, status_code=400)
    fields.append("updated_at = CURRENT_TIMESTAMP")
    values.append(int(mid))

    sql = f"UPDATE app_menus SET {', '.join(fields)} WHERE id = %s"
    params = tuple(values)

    def _do():
        with db_connect() as conn:
            conn.execute(sql, params)

    try:
        await asyncio.to_thread(_do)
        invalidate_nav_menu_cache()
        logger.info(f"[menu/update] id={mid}")
        return JSONResponse(
            {"ok": True},
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        msg = str(e).lower()
        if "unique" in msg or "duplicate" in msg:
            return JSONResponse(
                {"ok": False, "error": "path_already_exists"}, status_code=409
            )
        logger.warning(f"[menu/update] {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── POST /api/menu/admin/delete ────────────────────────────
@router.post("/api/menu/admin/delete")
async def delete_menu(request: Request):
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    mid = body.get("id")
    if not mid:
        return JSONResponse({"ok": False, "error": "id_required"}, status_code=400)

    def _do():
        with db_connect() as conn:
            conn.execute("DELETE FROM app_menus WHERE id = %s", (int(mid),))

    try:
        await asyncio.to_thread(_do)
        invalidate_nav_menu_cache()
        logger.info(f"[menu/delete] id={mid}")
        return JSONResponse(
            {"ok": True},
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        logger.warning(f"[menu/delete] {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


# ── POST /api/menu/admin/reorder ───────────────────────────
@router.post("/api/menu/admin/reorder")
async def reorder_menu(request: Request):
    """批量更新 sort_order: { items: [{id, sort_order}, ...] }"""
    if not _require_admin(request):
        return JSONResponse({"ok": False, "error": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid_json"}, status_code=400)
    items = body.get("items") or []
    if not isinstance(items, list):
        return JSONResponse(
            {"ok": False, "error": "items_must_be_list"}, status_code=400
        )

    def _do():
        with db_connect() as conn:
            for it in items:
                try:
                    iid = int(it.get("id"))
                    so = int(it.get("sort_order"))
                except Exception:
                    continue
                conn.execute(
                    "UPDATE app_menus SET sort_order = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                    (so, iid),
                )

    try:
        await asyncio.to_thread(_do)
        logger.info(f"[menu/reorder] count={len(items)}")
        return JSONResponse(
            {"ok": True, "count": len(items)},
            headers={"Cache-Control": "no-store"},
        )
    except Exception as e:
        logger.warning(f"[menu/reorder] {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
